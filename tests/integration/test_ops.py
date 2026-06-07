"""Ops dashboard surface — heartbeat-derived liveness, the queue rollup, log-link building, and the
``GET /ops/*`` HTTP endpoints (eval_engine/ops.py). Runs against the real Postgres + ClickHouse; the
network probes for off-box services (redis/litellm/viewer) resolve to ``down``/``unknown`` here, which
is exactly the graceful-degradation contract the snapshot must honor without throwing.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from eval_engine import control, ops, runner
from eval_engine.api import app
from eval_engine.models import RunSpec


@pytest.fixture
def client():
    return TestClient(app)


SPEC = RunSpec(
    eval="capitals_qa", dataset="examples/qa.jsonl", model="mockllm/model", mock_output="Paris",
    harness={"type": "single_turn"}, scorers=[{"type": "includes"}],
)


def test_run_live_per_sample_and_worker_links(client, monkeypatch):
    run_id = runner.launch(SPEC)            # creates run + expands the ledger (all queued)
    # claim one sample so it goes 'running' with a claiming worker pod recorded
    claimed = control.claim_batch(run_id, "eval-engine-worker-abc123-xy", 1)
    assert claimed

    monkeypatch.setattr(ops, "GCP_PROJECT", "my-proj")
    live = client.get(f"/runs/{run_id}/live").json()
    assert live["agentic"] is False
    by_id = {s["sample_id"]: s for s in live["samples"]}
    running = [s for s in live["samples"] if s["status"] == "running"]
    assert len(running) == 1
    r = running[0]
    assert r["claimed_by"] == "eval-engine-worker-abc123-xy"
    assert r["worker_logs_url"] and "abc123" in r["worker_logs_url"]   # links to the claiming pod
    assert r["sandbox_logs_url"] is None                              # not an agentic run
    # a still-queued sample has no logs link
    queued = [s for s in live["samples"] if s["status"] == "queued"]
    assert queued and queued[0]["worker_logs_url"] is None

    assert client.get("/runs/nope/live").status_code == 404


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

    # A live worker with no queued work ⇒ ok.
    control.heartbeat("worker", "w-1", {"claimed_this_loop": 4})
    hbs = control.list_heartbeats()
    assert ops.probe_workers(hbs, {"ledger": {"queued": 0}})["status"] == "ok"
    # Nothing live + nothing queued ⇒ idle (scaled to 0).
    assert ops.probe_workers([], {"ledger": {"queued": 0}})["status"] == "idle"

    # The crux: queued work with no live worker is AMBIGUOUS from Postgres alone — separate a normal
    # KEDA scale-from-0 (`scaling`) from a real stall/fault (`degraded`).
    backlog = {"ledger": {"queued": 7}}
    # fresh backlog, KEDA hasn't created a pod yet ⇒ scaling, NOT degraded (the false-alarm we fixed).
    assert ops.probe_workers([], backlog, queued_age_s=5)["status"] == "scaling"
    # a pod is being created (cold start) ⇒ scaling, regardless of queue age.
    creating = [{"phase": "Pending", "ready": False, "restarts": 0, "reason": "ContainerCreating"}]
    assert ops.probe_workers([], backlog, creating, queued_age_s=5)["status"] == "scaling"
    # work has waited past the grace window with nothing scheduled ⇒ a genuine stall ⇒ degraded.
    stalled = ops.probe_workers([], backlog, queued_age_s=ops.WORKER_SCALEUP_GRACE_S + 60)
    assert stalled["status"] == "degraded" and "KEDA" in stalled["detail"]
    # a crash-looping pod ⇒ degraded with the reason named (not mistaken for a slow scale-up).
    crash = [{"phase": "Running", "ready": False, "restarts": 6, "reason": "CrashLoopBackOff"}]
    crashed = ops.probe_workers([], backlog, crash, queued_age_s=5)
    assert crashed["status"] == "degraded" and "CrashLoopBackOff" in crashed["detail"]
    # No fresh heartbeat but K8s shows a ready pod ⇒ busy, not down (the worker-blocked-on-gateway case).
    busy = ops.probe_workers([], backlog, [{"phase": "Running", "ready": True, "restarts": 0, "reason": None}])
    assert busy["status"] == "ok" and "busy" in busy["detail"]


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
