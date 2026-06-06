"""Ops dashboard surface — heartbeat-derived liveness, the queue rollup, log-link building, and the
``GET /ops/*`` HTTP endpoints (eval_engine/ops.py). Runs against the real Postgres + ClickHouse; the
network probes for off-box services (redis/litellm/viewer) resolve to ``down``/``unknown`` here, which
is exactly the graceful-degradation contract the snapshot must honor without throwing.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from eval_engine import control, ops
from eval_engine.api import app


@pytest.fixture
def client():
    return TestClient(app)


def test_log_url_built_and_gated(monkeypatch):
    # No project configured ⇒ no link (dev), so the UI hides the button.
    monkeypatch.setattr(ops, "GCP_PROJECT", None)
    assert ops.log_url(app="eval-engine-orch") is None

    monkeypatch.setattr(ops, "GCP_PROJECT", "my-proj")
    monkeypatch.setattr(ops, "GKE_CLUSTER", "eval-engine")
    url = ops.log_url(container="worker", run_id="abc123", severity="ERROR", minutes=30)
    assert url.startswith("https://console.cloud.google.com/logs/query;query=")
    assert "project=my-proj" in url and "duration=PT30M" in url
    # the LQL filter is URL-encoded into the ;query= segment
    assert "container_name" in url.replace("%22", '"').replace("%3D", "=")
    assert "abc123" in url and "severity" in url.replace("%3E", ">")


def test_heartbeat_liveness(monkeypatch):
    # A fresh leader heartbeat ⇒ orchestrator ok + leader identity surfaced.
    control.heartbeat("orchestrator", "orch-0", {"leader": True, "running_runs": 2})
    hbs = control.list_heartbeats()
    comp = ops.probe_orchestrator(hbs)
    assert comp["status"] == "ok" and "orch-0" in comp["detail"]
    assert comp["metrics"]["running_runs"] == 2

    # A live worker with no queued work ⇒ ok; zero live workers with a backlog ⇒ degraded.
    control.heartbeat("worker", "w-1", {"claimed_this_loop": 4})
    hbs = control.list_heartbeats()
    assert ops.probe_workers(hbs, {"ledger": {"queued": 0}})["status"] == "ok"
    assert ops.probe_workers([], {"ledger": {"queued": 7}})["status"] == "degraded"
    assert ops.probe_workers([], {"ledger": {"queued": 0}})["status"] == "idle"


def test_snapshot_shape_and_degrades_gracefully(client):
    control.heartbeat("orchestrator", "orch-0", {"leader": True, "running_runs": 0})
    snap = client.get("/ops/status").json()
    assert snap["overall"] in ("ok", "degraded", "down")
    names = {c["name"] for c in snap["components"]}
    # the full component set is always present, even when a backend is unreachable
    assert {"api", "orchestrator", "workers", "postgres", "clickhouse", "litellm"} <= names
    # critical backends are reachable in the test env ⇒ never a hard "down" overall
    pg = next(c for c in snap["components"] if c["name"] == "postgres")
    assert pg["status"] == "ok" and "ledger_rows" in pg["metrics"]
    for key in ("cluster", "queues", "active_runs", "failures", "audit", "generated_at"):
        assert key in snap


def test_ops_logs_endpoint(client, monkeypatch):
    monkeypatch.setattr(ops, "GCP_PROJECT", "my-proj")
    body = client.get("/ops/logs", params={"run_id": "deadbeef"}).json()
    assert body["url"] and "deadbeef" in body["url"]
    monkeypatch.setattr(ops, "GCP_PROJECT", None)
    assert client.get("/ops/logs", params={"component": "redis"}).json()["url"] is None
