"""FastAPI control plane via TestClient — the HTTP surface end to end.

Drives the real app against the real backends (the ``clean_db`` autouse fixture in this dir resets
state per test). Covers: health/catalog, the two-plane launch (validate → create → expand, 202),
rerun, the results/transcript read path (executed inline via ``runner.run``), the entity registry
CRUD with its 422 validation, and the audit trail.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from eval_engine import runner
from eval_engine.api import app

# A deterministic mock RunSpec as the POST /runs body (no API key; 'Paris' → 1/3 on qa.jsonl).
SPEC = {
    "eval": "capitals_qa", "dataset": "examples/qa.jsonl", "model": "mockllm/model",
    "mock_output": "Paris", "batch_size": 2,
    "harness": {"type": "single_turn"},
    "scorers": [{"type": "includes", "config": {"ignore_case": True}}],
}


@pytest.fixture
def client():
    return TestClient(app)


def test_health_and_catalog(client):
    h = client.get("/healthz")
    assert h.status_code == 200 and h.json()["status"] == "ok"
    cat = client.get("/catalog").json()
    names = {(p["kind"], p["name"]) for p in cat}
    assert ("harness", "single_turn") in names and ("scorer", "includes") in names


def test_create_run_lists_and_404(client):
    r = client.post("/runs", json=SPEC, headers={"X-Auth-Request-Email": "me@x.com"})
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    assert r.json()["status"] == "queued"

    listed = client.get("/runs").json()
    row = next(x for x in listed if x["id"] == run_id)
    assert row["eval"] == "capitals_qa" and row["created_by"] == "me@x.com"

    meta = client.get(f"/runs/{run_id}").json()
    assert meta["eval_id"] == "capitals_qa" and meta["total"] == 3 and "progress" in meta

    assert client.get("/runs/nope").status_code == 404


def test_create_run_validates_plugins(client):
    bad = {**SPEC, "harness": {"type": "does_not_exist"}}
    assert client.post("/runs", json=bad).status_code == 422


def test_rerun_clones_and_404(client):
    run_id = client.post("/runs", json=SPEC).json()["run_id"]
    r = client.post(f"/runs/{run_id}/rerun")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["rerun_of"] == run_id and body["run_id"] != run_id
    # the clone carries the same pinned inputs
    assert client.get(f"/runs/{body['run_id']}").json()["eval_id"] == "capitals_qa"

    assert client.post("/runs/nope/rerun").status_code == 404


def test_results_and_transcript(client):
    # Execute a run to completion inline, then read it back through the HTTP surface.
    run_id = runner.run(runner.RunSpec(**SPEC))

    res = client.get(f"/runs/{run_id}/results")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["summary"]["samples"] == 3 and body["summary"]["passed"] == 1
    assert len(body["summary"]["accuracy_ci"]) == 2
    assert len(body["samples"]) == 3 and {s["passed"] for s in body["samples"]} == {0, 1}

    uri = next(s["transcript_uri"] for s in body["samples"] if s["transcript_uri"])
    t = client.get("/transcript", params={"uri": uri})
    assert t.status_code == 200 and "output" in t.json()
    assert client.get("/transcript", params={"uri": "/no/such/file.json"}).status_code == 404

    assert client.get("/runs/nope/results").status_code == 404


def test_entity_registry_crud_and_validation(client):
    # dataset: register → list → get → 404
    ds = {"id": "capitals", "uri": "examples/qa.jsonl"}
    assert client.post("/datasets", json=ds).status_code == 201
    assert any(e["id"] == "capitals" for e in client.get("/datasets").json())
    assert client.get("/datasets/capitals").json()["id"] == "capitals"
    assert client.get("/datasets/missing").status_code == 404

    # eval bundle validates its referenced plugins (422 on a bad scorer)
    good_eval = {"id": "capitals_qa", "dataset": "capitals",
                 "default_harness": {"type": "single_turn"},
                 "default_scorers": [{"type": "includes"}]}
    assert client.post("/evals", json=good_eval).status_code == 201
    bad_eval = {**good_eval, "default_scorers": [{"type": "nope"}]}
    assert client.post("/evals", json=bad_eval).status_code == 422

    # model
    model = {"id": "gpt-4o-mini-prod", "provider": "openrouter", "model_id": "openai/gpt-4o-mini"}
    assert client.post("/models", json=model).status_code == 201
    # GET returns the entity envelope {id, version, body, …}; the spec fields live under "body"
    assert client.get("/models/gpt-4o-mini-prod").json()["body"]["model_id"] == "openai/gpt-4o-mini"


def test_audit_records_launch(client):
    run_id = client.post("/runs", json=SPEC, headers={"X-Auth-Request-Email": "auditor@x"}).json()["run_id"]
    entries = client.get("/audit").json()
    launch = next(e for e in entries if e["action"] == "run.launch" and e["target"] == run_id)
    assert launch["actor"] == "auditor@x" and launch["detail"]["eval"] == "capitals_qa"
