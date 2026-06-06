"""Shared pytest fixtures.

The DB fixtures here are deliberately NOT autouse — unit tests (``tests/unit/``) must stay I/O-free.
The ``tests/integration/`` and ``tests/e2e/`` conftests opt every test in that dir into ``clean_db``,
which TRUNCATEs the control tables before each test so tests never share state (the flakiness the old
ad-hoc suite had).

The backends are self-provisioned: the ``_backends`` session fixture (pulled in transitively by every
integration/e2e test, never by unit tests) reuses a reachable Postgres + ClickHouse if one is already
up (CI service containers, or a dev who ran ``infra/up.sh``) and otherwise starts the docker stack
itself, tearing down only what it started. So ``pytest`` "just works" with no manual step.
"""
from __future__ import annotations

import os
import subprocess
import urllib.request
from pathlib import Path

import psycopg
import pytest

from eval_engine import analytics, control

_REPO_ROOT = Path(__file__).resolve().parent.parent


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
_CONTROL_TABLES = ("runs, sample_tasks, failed_task_archive, entities, audit_log, heartbeats, "
                   "training_runs, checkpoints, checkpoint_scores, anomalies, checkpoint_models")


def _backends_reachable() -> bool:
    """True iff Postgres AND ClickHouse both accept a connection at the app's configured endpoints."""
    try:
        psycopg.connect(control.DSN, connect_timeout=2).close()
    except Exception:
        return False
    host = os.environ.get("EVAL_ENGINE_CH_HOST", "localhost")
    port = os.environ.get("EVAL_ENGINE_CH_PORT", "8123")
    try:
        urllib.request.urlopen(f"http://{host}:{port}/ping", timeout=2).read()
    except Exception:
        return False
    return True


@pytest.fixture(scope="session")
def _backends():
    """Ensure the backends are up for the whole session. Reuse them if already reachable; otherwise
    start the docker stack via ``infra/up.sh`` (the single source of container config) and stop it
    via ``infra/down.sh`` at session end. Only what *this* fixture started is torn down — a dev's
    own ``infra/up.sh`` containers are left running."""
    if _backends_reachable():
        yield
        return
    subprocess.run(["bash", str(_REPO_ROOT / "infra" / "up.sh")], check=True)  # waits for readiness
    try:
        yield
    finally:
        subprocess.run(["bash", str(_REPO_ROOT / "infra" / "down.sh")], check=True)


@pytest.fixture(scope="session")
def _schema(_backends):
    """Create the Postgres + ClickHouse schema once per session."""
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


@pytest.fixture
def mock_spec():
    """Factory for a deterministic single_turn mock RunSpec (no API key, always answers 'Paris' →
    1/3 on examples/qa.jsonl). Override any field via kwargs, e.g. ``mock_spec(epochs=3)``."""
    from eval_engine.models import PluginRef, RunSpec

    def _make(**over) -> RunSpec:
        base = dict(
            eval="capitals_qa", dataset="examples/qa.jsonl", model="mockllm/model",
            mock_output="Paris", batch_size=2,
            harness=PluginRef(type="single_turn"),
            scorers=[PluginRef(type="includes", config={"ignore_case": True})],
        )
        base.update(over)
        return RunSpec(**base)
    return _make
