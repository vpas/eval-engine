"""Leader election over a real Postgres advisory lock — the split-brain mutex (docs/RESILIENCE.md §4.2).

This is the code path the orchestrator-HA bump exposed in production: a transaction-pooled endpoint
hands the "same" session lock to every replica → two leaders tick admit/finalize at once. Against a
SESSION-mode Postgres (the local docker one) the lock is a true cross-replica mutex; these tests pin
that contract directly against the backend:

  - ``acquire_leader`` is a single-holder mutex (a second connection is refused while it's held),
  - ``release_leader`` frees it for a standby (graceful handover),
  - ``reap_stale_leader`` terminates an *idle* holder (the ungraceful-death backstop, bug B1) but
    SPARES one that hasn't been idle past the threshold (the live-leader guard).

Each test uses a fresh random advisory key so nothing leaks between tests or from a prior run, and the
module's dedicated leader connection (``control._leader_con``) is reset + released in the fixture.
"""
from __future__ import annotations

import random
import time

import psycopg
import pytest

from eval_engine import control


@pytest.fixture
def leader_key(_schema):
    """A unique advisory key per test + guaranteed release of the module leader connection after."""
    key = random.randint(0x1000_0000, 0x7FFF_FFFF)  # distinct from the orchestrator's real LEADER_KEY
    control._leader_con = None
    yield key
    control.release_leader(key)
    control._leader_con = None


def _contender() -> psycopg.Connection:
    """An independent session-mode connection that races for the same lock (a 'second replica')."""
    return psycopg.connect(control._leader_dsn(), autocommit=True)


def _try_lock(con: psycopg.Connection, key: int) -> bool:
    return bool(con.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0])


def test_acquire_is_a_single_holder_mutex(leader_key):
    assert control.acquire_leader(leader_key) is True
    assert control.leader_alive() is True
    # A second replica must be REFUSED while we hold the lock — the property a pooler would break.
    other = _contender()
    try:
        assert _try_lock(other, leader_key) is False
    finally:
        other.close()


def test_release_hands_the_lock_to_a_standby(leader_key):
    assert control.acquire_leader(leader_key) is True
    control.release_leader(leader_key)          # graceful SIGTERM-style handover
    control._leader_con = None
    standby = _contender()
    try:
        assert _try_lock(standby, leader_key) is True   # now acquirable by the standby
        standby.execute("SELECT pg_advisory_unlock(%s)", (leader_key,))
    finally:
        standby.close()


def test_reap_terminates_an_idle_foreign_holder(leader_key):
    # Simulate an ungracefully-dead leader: a connection that grabbed the lock and went idle holding it.
    zombie = _contender()
    assert _try_lock(zombie, leader_key) is True
    # idle_seconds=0 → any idle holder qualifies; loop briefly so state_change is strictly in the past.
    reaped = 0
    for _ in range(40):
        reaped = control.reap_stale_leader(leader_key, idle_seconds=0)
        if reaped:
            break
        time.sleep(0.05)
    assert reaped >= 1
    zombie.close()
    # With the zombie reaped the lock is free, so a standby (us) can take over.
    assert control.acquire_leader(leader_key) is True


def test_reap_spares_a_holder_within_the_idle_threshold(leader_key):
    # The live-leader guard: a holder idle for LESS than the threshold must not be reaped (a healthy
    # leader refreshes its connection every tick, so it never idles past the window).
    holder = _contender()
    assert _try_lock(holder, leader_key) is True
    try:
        assert control.reap_stale_leader(leader_key, idle_seconds=9999) == 0
        # Still held → we cannot acquire it.
        assert control.acquire_leader(leader_key) is False
    finally:
        holder.execute("SELECT pg_advisory_unlock(%s)", (leader_key,))
        holder.close()


def test_leader_alive_false_when_no_connection(leader_key):
    control._leader_con = None
    assert control.leader_alive() is False
