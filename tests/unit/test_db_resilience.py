"""Unit: the storage-tier connection-resilience helpers (docs/RESILIENCE.md item A) — pure, no backends.

``control._run`` / ``analytics._run`` must transparently reconnect + retry a query when the underlying
connection has been dropped underneath us (Neon failover/cold-start, idle reap, NAT timeout), instead of
letting the OperationalError/InterfaceError propagate and crash the worker/orchestrator loop. They must
also NOT retry an unrelated error (a real query/programming bug), which retrying would only mask + slow.
"""
import contextlib

import psycopg
import pytest
from clickhouse_connect.driver.exceptions import OperationalError as CHOperationalError

from eval_engine import analytics, control


class _FakePool:
    """Stand-in for the psycopg_pool ConnectionPool: ``.connection()`` hands out a dummy connection per
    checkout (a fresh one each attempt, mirroring how the real pool replaces a dropped connection)."""
    @contextlib.contextmanager
    def connection(self):
        yield object()


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    # Don't actually sleep between retries — keep the unit test instant.
    monkeypatch.setattr(control.time, "sleep", lambda _s: None)
    monkeypatch.setattr(analytics.time, "sleep", lambda _s: None)


# --------------------------------------------------------------------------- control (Postgres)

def test_control_run_reconnects_and_retries(monkeypatch):
    # A dropped connection on the first attempt → the pool hands out a fresh connection on retry → the
    # second attempt succeeds. (The pool discards the broken connection on block exit; _run just retries.)
    monkeypatch.setattr(control, "_get_pool", lambda: _FakePool())
    n = {"calls": 0}

    def op(_con):
        n["calls"] += 1
        if n["calls"] == 1:
            raise psycopg.OperationalError("connection reset")
        return "ok"

    assert control._run(op) == "ok"
    assert n["calls"] == 2  # retried exactly once on the dropped connection


def test_control_run_gives_up_after_max_tries(monkeypatch):
    # A persistently-dead connection still surfaces the error (bounded retries, then re-raise).
    monkeypatch.setattr(control, "_get_pool", lambda: _FakePool())
    n = {"calls": 0}

    def op(_con):
        n["calls"] += 1
        raise psycopg.InterfaceError("still down")

    with pytest.raises(psycopg.InterfaceError):
        control._run(op)
    assert n["calls"] == control._RETRY_TRIES


def test_control_run_does_not_retry_unrelated_errors(monkeypatch):
    # A real query/programming bug is not a connection problem — propagate immediately, don't mask it.
    monkeypatch.setattr(control, "_get_pool", lambda: _FakePool())
    n = {"calls": 0}

    def op(_con):
        n["calls"] += 1
        raise ValueError("bad query")

    with pytest.raises(ValueError):
        control._run(op)
    assert n["calls"] == 1


# --------------------------------------------------------------------------- leader DSN (session mode)

def test_leader_dsn_strips_neon_pooler_suffix(monkeypatch):
    # The leader's advisory lock needs a session-mode endpoint; Neon's direct host is the pooled host
    # without `-pooler`. (A transaction pooler would hand the "lock" to every replica → split-brain.)
    monkeypatch.delenv("EVAL_ENGINE_PG_LEADER_DSN", raising=False)
    monkeypatch.setattr(control, "DSN",
                        "postgresql://u:p@ep-ancient-pine-a6roa0ut-pooler.us-west-2.aws.neon.tech/db")
    assert control._leader_dsn() == \
        "postgresql://u:p@ep-ancient-pine-a6roa0ut.us-west-2.aws.neon.tech/db"


def test_leader_dsn_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("EVAL_ENGINE_PG_LEADER_DSN", "host=direct.example port=5432")
    monkeypatch.setattr(control, "DSN", "host=whatever-pooler.example port=5432")
    assert control._leader_dsn() == "host=direct.example port=5432"


def test_leader_dsn_noop_for_non_pooled(monkeypatch):
    # Local / non-pooled DSN is returned unchanged.
    monkeypatch.delenv("EVAL_ENGINE_PG_LEADER_DSN", raising=False)
    monkeypatch.setattr(control, "DSN", "host=localhost port=5433 dbname=evalengine")
    assert control._leader_dsn() == "host=localhost port=5433 dbname=evalengine"


# --------------------------------------------------------------------------- analytics (ClickHouse)

def test_analytics_run_drops_client_and_retries(monkeypatch):
    # On a connection error the cached client is reset to None (so _c rebuilds it) and the op retries.
    analytics._client = "stale-client"
    monkeypatch.setattr(analytics, "_c", lambda: "fresh-client")
    n = {"calls": 0}
    client_seen = []

    def op(_client):
        n["calls"] += 1
        client_seen.append(analytics._client)  # snapshot the cached global at each attempt
        if n["calls"] == 1:
            raise CHOperationalError("ch unreachable")
        return 42

    assert analytics._run(op) == 42
    assert n["calls"] == 2
    # first attempt saw the stale client; by the retry the failure path had reset it to None
    assert client_seen == ["stale-client", None]


def test_analytics_run_does_not_retry_unrelated_errors(monkeypatch):
    monkeypatch.setattr(analytics, "_c", lambda: "client")
    n = {"calls": 0}

    def op(_client):
        n["calls"] += 1
        raise ValueError("bad query")

    with pytest.raises(ValueError):
        analytics._run(op)
    assert n["calls"] == 1
