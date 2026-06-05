"""FastAPI control plane (Phase 1 slice).

Mirrors the two-plane design: ``POST /runs`` is the control-plane action (validate + create
run + expand ledger, return immediately); execution runs in the background (the orchestrator
role). Progress is polled from the ledger; results come from the analytics store.

Run:  PYTHONPATH=. ../.venv/bin/uvicorn eval_engine.api:app --port 8077
Docs: http://localhost:8077/docs
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, Response

from . import builtins, db, plugins, runner  # noqa: F401  populate registry
from .db import analytics, control
from .models import DatasetSpec, EvalSpec, ModelSpec, RunSpec

# In the cluster the API is control-plane only — it launches (creates run + expands ledger) and the
# orchestrator/worker pods execute. Local single-process dev (sqlite) keeps the convenient inline
# background execute so the dashboard works without standing up separate processes.
INLINE_EXEC = os.environ.get(
    "EVAL_ENGINE_API_INLINE_EXEC", "1" if db.BACKEND == "sqlite" else "0"
) == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure schema once the DB is reachable. A pod may start before Neon/ClickHouse accept
    # connections (cold start / ordering), so retry with backoff instead of crash-looping.
    last = None
    for attempt in range(12):
        try:
            db.init()
            break
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(1.5 ** attempt, 8))
    else:
        raise RuntimeError(f"databases not reachable at startup: {last}")
    yield


app = FastAPI(title="eval-engine", version="0.1.0-prototype", lifespan=lifespan)
_UI = Path(__file__).resolve().parent.parent / "static" / "index.html"


@app.get("/", response_class=HTMLResponse)
def ui():
    return _UI.read_text()


@app.get("/healthz")
def health():
    return {"status": "ok", "service": "eval-engine", "live_ledger_rows": control.ledger_size()}


@app.get("/catalog")
def catalog():
    """Registered harnesses/scorers (what the launch wizard renders from)."""
    return plugins.catalog()


@app.post("/runs", status_code=202)
def create_run(spec: RunSpec, bg: BackgroundTasks,
               x_auth_request_email: str | None = Header(default=None)):
    """Validate + create the run, expand the ledger, kick off background execution.

    ``X-Auth-Request-Email`` is injected by the OIDC proxy (oauth2-proxy) — the authenticated
    user, recorded as the run's ``created_by``. Absent on the internal/port-forward path.
    """
    try:
        plugins.get("harness", spec.harness.type, spec.harness.version)
        for s in spec.scorers:
            plugins.get("scorer", s.type, s.version)
    except KeyError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None

    run_id = runner.launch(spec, created_by=x_auth_request_email)
    if INLINE_EXEC:
        bg.add_task(runner.execute, run_id, spec)  # local dev only; cluster uses orchestrator+workers
    return {"run_id": run_id, "status": "queued"}


@app.post("/runs/{run_id}/rerun", status_code=202)
def rerun(run_id: str, bg: BackgroundTasks, x_auth_request_email: str | None = Header(default=None)):
    """Reproduce a past run (FR10, §9.9): clone its stored RunSpec → a new Run with identical pinned
    inputs (eval@version, dataset content hash, model + params + seed, epochs, budget, image digest)."""
    spec_json = control.get_spec(run_id)
    if not spec_json:
        raise HTTPException(status_code=404, detail=f"no run {run_id}")
    spec = RunSpec.model_validate_json(spec_json)
    new_id = runner.launch(spec, created_by=x_auth_request_email)
    if INLINE_EXEC:
        bg.add_task(runner.execute, new_id, spec)
    return {"run_id": new_id, "status": "queued", "rerun_of": run_id}


@app.get("/runs")
def list_runs():
    cols = ["id", "eval", "model", "accuracy", "total", "created_at", "created_by", "status"]
    return [dict(zip(cols, r)) for r in control.list_runs()]


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    run = control.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"no run {run_id}")
    # Keep in sync with control.RUN_COLS (explicit select; the table has more columns than we map).
    cols = [
        "id", "eval_id", "eval_version", "model", "provider", "model_id", "harness", "scorers",
        "status", "total", "done", "failed", "accuracy", "cost_usd", "dataset_hash", "created_by",
        "team", "image_digest", "created_at", "finished_at",
    ]
    meta = dict(zip(cols, run))
    # live progress from the ledger (empty once finalized/pruned). done/failed/accuracy/cost_usd on the
    # row are the orchestrator's live rollup (DESIGN §8) — authoritative live *and* final.
    meta["progress"] = control.counts(run_id)
    return meta


@app.get("/transcript")
def transcript(uri: str):
    """Serve a sample transcript by URI (GCS in-cluster, local in dev) — dashboard drill-in."""
    body = runner.get_transcript(uri)
    if body is None:
        raise HTTPException(status_code=404, detail="transcript not available")
    return Response(content=body, media_type="application/json")


@app.get("/runs/{run_id}/results")
def get_results(run_id: str):
    if not control.get_run(run_id):
        raise HTTPException(status_code=404, detail=f"no run {run_id}")
    n, passed, mean, tokens, cost = analytics.run_summary(run_id)
    ci_lo, ci_hi = runner.wilson_ci(int(passed), int(n))  # 95% Wilson CI on the pass rate (FR8)
    return {
        "summary": {
            "samples": n, "passed": passed, "accuracy": (passed / n) if n else 0.0,
            "accuracy_ci": [ci_lo, ci_hi],
            "mean_score": mean, "tokens": tokens, "cost_usd": cost,
        },
        "by_category": [
            {"category": gk or "", "n": c, "passed": p, "accuracy": acc}
            for gk, c, p, acc in analytics.by_category(run_id)
        ],
        "samples": [
            {"sample_id": sid, "passed": p, "category": gk, "score": sc, "transcript_uri": uri}
            for sid, p, gk, sc, uri in analytics.samples(run_id)
        ],
    }


# --- Entity registry (FR1–3): register + list + get for datasets / evals / models. Versions are
# immutable (register a new version rather than mutate), so there is no PUT/DELETE — the reproducible,
# content-addressed stance of DESIGN §13/§14. POSTs are typed (Pydantic) so the body is validated.

def _register(kind: str, spec, email: str | None):
    control.register_entity(kind, spec.id, spec.version, spec.model_dump(), email)
    return {"id": spec.id, "version": spec.version}


def _get(kind: str, ent_id: str):
    e = control.get_entity(kind, ent_id)
    if not e:
        raise HTTPException(status_code=404, detail=f"no {kind} {ent_id}")
    return e


@app.post("/datasets", status_code=201)
def register_dataset(spec: DatasetSpec, x_auth_request_email: str | None = Header(default=None)):
    # Content-address the data (FR1, §13): snapshot the bytes to immutable storage + pin the hash, so
    # the version is reproducible by content. Best-effort — if the uri isn't readable from the API
    # (e.g. a client-side path), register the metadata as-is.
    from .datasets import snapshot
    try:
        content_hash, snapshot_uri = snapshot(spec.uri)
        spec = spec.model_copy(update={"content_hash": content_hash, "snapshot_uri": snapshot_uri})
    except Exception:  # noqa: BLE001
        pass
    return _register("dataset", spec, x_auth_request_email)


@app.get("/datasets")
def list_datasets():
    return control.list_entities("dataset")


@app.get("/datasets/{ds_id}")
def get_dataset(ds_id: str):
    return _get("dataset", ds_id)


@app.post("/evals", status_code=201)
def register_eval(spec: EvalSpec, x_auth_request_email: str | None = Header(default=None)):
    # Validate referenced plugins exist (an eval bundles a harness + scorers).
    try:
        plugins.get("harness", spec.default_harness.type, spec.default_harness.version)
        for s in spec.default_scorers:
            plugins.get("scorer", s.type, s.version)
    except KeyError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return _register("eval", spec, x_auth_request_email)


@app.get("/evals")
def list_evals():
    return control.list_entities("eval")


@app.get("/evals/{eval_id}")
def get_eval(eval_id: str):
    return _get("eval", eval_id)


@app.post("/models", status_code=201)
def register_model(spec: ModelSpec, x_auth_request_email: str | None = Header(default=None)):
    return _register("model", spec, x_auth_request_email)


@app.get("/models")
def list_models():
    return control.list_entities("model")


@app.get("/models/{model_id}")
def get_model(model_id: str):
    return _get("model", model_id)
