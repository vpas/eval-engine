"""Unit: the storage-tier connection-resilience helpers (docs/RESILIENCE.md item A) — pure, no backends.

``control._run`` / ``analytics._run`` must transparently reconnect + retry a query when the underlying
connection has been dropped underneath us (Neon failover/cold-start, idle reap, NAT timeout), instead of
letting the OperationalError/InterfaceError propagate and crash the worker/orchestrator loop. They must
also NOT retry an unrelated error (a real query/programming bug), which retrying would only mask + slow.
"""
import psycopg
import pytest
from clickhouse_connect.driver.exceptions import OperationalError as CHOperationalError

from eval_engine import analytics, control


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    # Don't actually sleep between retries — keep the unit test instant.
    monkeypatch.setattr(control.time, "sleep", lambda _s: None)
    monkeypatch.setattr(analytics.time, "sleep", lambda _s: None)


# --------------------------------------------------------------------------- control (Postgres)

def test_control_run_reconnects_and_retries(monkeypatch):
    # A dropped connection on the first attempt → drop + reconnect + retry → second attempt succeeds.
    monkeypatch.setattr(control, "_raw", lambda: object())
    dropped = []
    monkeypatch.setattr(control, "_drop", lambda: dropped.append(True))
    n = {"calls": 0}

    def op(_con):
        n["calls"] += 1
        if n["calls"] == 1:
            raise psycopg.OperationalError("connection reset")
        return "ok"

    assert control._run(op) == "ok"
    assert n["calls"] == 2 and dropped == [True]  # reconnected exactly once


def test_control_run_gives_up_after_max_tries(monkeypatch):
    # A persistently-dead connection still surfaces the error (bounded retries, then re-raise).
    monkeypatch.setattr(control, "_raw", lambda: object())
    monkeypatch.setattr(control, "_drop", lambda: None)
    n = {"calls": 0}

    def op(_con):
        n["calls"] += 1
        raise psycopg.InterfaceError("still down")

    with pytest.raises(psycopg.InterfaceError):
        control._run(op)
    assert n["calls"] == control._RETRY_TRIES


def test_control_run_does_not_retry_unrelated_errors(monkeypatch):
    # A real query/programming bug is not a connection problem — propagate immediately, don't mask it.
    monkeypatch.setattr(control, "_raw", lambda: object())
    monkeypatch.setattr(control, "_drop", lambda: None)
    n = {"calls": 0}

    def op(_con):
        n["calls"] += 1
        raise ValueError("bad query")

    with pytest.raises(ValueError):
        control._run(op)
    assert n["calls"] == 1


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
