"""Shared pytest fixtures.

The DB fixtures here are deliberately NOT autouse — unit tests (``tests/unit/``) must stay I/O-free.
The ``tests/integration/`` and ``tests/e2e/`` conftests opt every test in that dir into ``clean_db``,
which TRUNCATEs the control tables before each test so tests never share state (the flakiness the old
ad-hoc suite had). Connection defaults point at the local docker stack (``infra/up.sh``).
"""
from __future__ import annotations

import pytest

from eval_engine import analytics, control


def pytest_collection_modifyitems(items):
    """Auto-mark each test by the layer dir it lives in (unit/integration/e2e), so `pytest -m unit`
    (fast, no backends) and `pytest -m 'not e2e'` work without per-file boilerplate."""
    for item in items:
        path = str(item.fspath)
        for layer in ("unit", "integration", "e2e"):
            if f"/{layer}/" in path:
                item.add_marker(layer)

# The mutable control tables wiped between tests for isolation (analytics is run_id-scoped, so its
# rows don't cross-contaminate and don't need truncating).
_CONTROL_TABLES = "runs, sample_tasks, failed_task_archive, entities, audit_log"


@pytest.fixture(scope="session")
def _schema():
    """Create the Postgres + ClickHouse schema once per session (needs infra/up.sh)."""
    control.init()
    analytics.init()


@pytest.fixture
def clean_db(_schema):
    """Per-test isolation: wipe the control tables so no test sees another's rows."""
    control._conn().execute(f"TRUNCATE {_CONTROL_TABLES} RESTART IDENTITY")
    yield


@pytest.fixture
def fake_result():
    """Factory for a committed-sample result dict; override any field via kwargs."""
    def _make(**over) -> dict:
        return {"passed": 1, "primary_score": 1.0, "scores": {"includes": 1.0}, "tokens_in": 1,
                "tokens_out": 1, "cost_usd": 0.0, "latency_ms": 0, "error_type": "",
                "transcript_uri": "", **over}
    return _make


@pytest.fixture
def make_run(_schema):
    """Factory: create a queued run with ``n`` expanded sample-tasks; returns its run_id.
    Override any ``runs`` column via kwargs (e.g. ``max_inflight=3``, ``lane="batch"``)."""
    def _make(n: int = 5, **over) -> str:
        run_id = control.new_run_id()
        meta = {"id": run_id, "eval_id": "t", "eval_version": 1, "model": "x/y", "provider": "x",
                "model_id": "y", "harness": "single_turn", "scorers": [], "total": n,
                "dataset_hash": "-", **over}
        control.create_run(meta)
        control.expand_tasks(run_id, [(f"s{i:04d}", "cat") for i in range(n)])
        return run_id
    return _make
