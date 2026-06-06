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

from . import builtins, db, plugins, runner, training  # noqa: F401  populate registry
from .db import analytics, control
from .models import (DatasetSpec, EvalSpec, LaunchFromEval, ModelSpec, PluginRef, RunSpec,
                     TrainingRunSpec)

# In the cluster the API is control-plane only — it launches (creates run + expands ledger) and the
# orchestrator/worker pods execute. Set EVAL_ENGINE_API_INLINE_EXEC=1 for local single-process dev:
# the API runs the whole pipeline inline so the dashboard works without standing up separate
# orchestrator/worker processes. Off by default (the cluster shape).
INLINE_EXEC = os.environ.get("EVAL_ENGINE_API_INLINE_EXEC", "0") == "1"


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


@app.get("/audit")
def audit_log(limit: int = 100):
    """Append-only audit trail of mutating actions — who launched/re-ran/registered what (§8/§13)."""
    return control.list_audit(limit)


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
    control.audit(x_auth_request_email, "run.launch", run_id, {"eval": spec.eval, "model": spec.model})
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
    control.audit(x_auth_request_email, "run.rerun", new_id, {"rerun_of": run_id})
    if INLINE_EXEC:
        bg.add_task(runner.execute, new_id, spec)
    return {"run_id": new_id, "status": "queued", "rerun_of": run_id}


@app.get("/runs")
def list_runs():
    cols = ["id", "eval", "eval_version", "model", "accuracy", "total", "cost", "created_at",
            "created_by", "status", "sweep"]
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
        "team", "image_digest", "lane", "created_at", "finished_at", "provider_fingerprint",
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
            {"sample_id": sid, "passed": p, "category": gk, "score": sc, "transcript_uri": uri,
             "tokens": tok, "latency_ms": lat, "error_type": err or ""}
            for sid, p, gk, sc, uri, tok, lat, err in analytics.samples(run_id)
        ],
    }


# --- Entity registry (FR1–3): register + list + get for datasets / evals / models. Versions are
# immutable (register a new version rather than mutate), so there is no PUT/DELETE — the reproducible,
# content-addressed stance of DESIGN §13/§14. POSTs are typed (Pydantic) so the body is validated.

def _register(kind: str, spec, email: str | None):
    control.register_entity(kind, spec.id, spec.version, spec.model_dump(), email)
    control.audit(email, f"{kind}.register", spec.id, {"version": spec.version})
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


@app.post("/evals/{eval_id}/launch", status_code=202)
def launch_from_eval(eval_id: str, body: LaunchFromEval, bg: BackgroundTasks,
                     x_auth_request_email: str | None = Header(default=None)):
    """Launch a run from a registered eval: resolve its dataset (the pinned content-addressed snapshot)
    + default harness/scorers, apply the caller's model + run knobs, then launch (FR2/FR10)."""
    ev = control.get_entity("eval", eval_id)
    if not ev:
        raise HTTPException(status_code=404, detail=f"no eval {eval_id}")
    e = ev["body"]
    ds = control.get_entity("dataset", e["dataset"])
    if not ds:
        raise HTTPException(status_code=422, detail=f"eval {eval_id} references unregistered dataset {e['dataset']}")
    dataset_uri = ds["body"].get("snapshot_uri") or ds["body"]["uri"]  # prefer the immutable snapshot
    spec = RunSpec(
        eval=eval_id, eval_version=ev["version"], dataset=dataset_uri, model=body.model,
        harness=PluginRef(**e["default_harness"]),
        scorers=[PluginRef(**s) for s in e["default_scorers"]],
        batch_size=body.batch_size, limit=body.limit, epochs=body.epochs,
        budget_usd=body.budget_usd, mock_output=body.mock_output,
        temperature=body.temperature, seed=body.seed,
        transcript_sample_rate=body.transcript_sample_rate,
    )
    run_id = runner.launch(spec, created_by=x_auth_request_email)
    control.audit(x_auth_request_email, "run.launch_from_eval", run_id,
                  {"eval": eval_id, "version": ev["version"], "model": body.model})
    if INLINE_EXEC:
        bg.add_task(runner.execute, run_id, spec)
    return {"run_id": run_id, "status": "queued", "from_eval": eval_id, "eval_version": ev["version"]}


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


# --- Training monitor (docs/TRAINING_MONITOR.md). Register a training run to watch, then read its
# checkpoint trajectory / anomalies. The monitor role (eval_engine.training) discovers + fans out +
# reconciles on its own loop; these endpoints are the read surface (+ a manual scan for dev/test).
# The frontend for this lands with the full prototype-based dashboard rewrite — API only here.

@app.post("/training", status_code=201)
def register_training(spec: TrainingRunSpec, x_auth_request_email: str | None = Header(default=None)):
    """Register a training run to monitor. Its suite evals must already be registered evals."""
    for entry in spec.suite:
        if not control.get_entity("eval", entry.eval, entry.version):
            raise HTTPException(status_code=422, detail=f"suite eval not registered: {entry.eval}")
    training.register(spec)
    control.audit(x_auth_request_email, "training.register", spec.id,
                  {"model": spec.model, "suite": [e.eval for e in spec.suite]})
    return {"id": spec.id, "status": "watching"}


@app.get("/training")
def list_training():
    return control.list_training_runs()


@app.get("/training/{tr_id}")
def get_training(tr_id: str):
    tr = control.get_training_run(tr_id)
    if not tr:
        raise HTTPException(status_code=404, detail=f"no training run {tr_id}")
    tr["best_checkpoints"] = training.best_checkpoints(tr_id)
    tr["anomaly_count"] = len(control.list_anomalies(tr_id))
    return tr


@app.get("/training/{tr_id}/checkpoints")
def training_checkpoints(tr_id: str):
    if not control.get_training_run(tr_id):
        raise HTTPException(status_code=404, detail=f"no training run {tr_id}")
    return control.list_checkpoints(tr_id)


@app.get("/training/{tr_id}/series")
def training_series(tr_id: str):
    """The score-vs-step series per eval (accuracy + Wilson CI + expected band) — the chart's data."""
    if not control.get_training_run(tr_id):
        raise HTTPException(status_code=404, detail=f"no training run {tr_id}")
    series: dict[str, list] = {}
    for s in control.checkpoint_scores(tr_id):
        series.setdefault(s["eval_id"], []).append(s)
    return series


@app.get("/training/{tr_id}/anomalies")
def training_anomalies(tr_id: str):
    if not control.get_training_run(tr_id):
        raise HTTPException(status_code=404, detail=f"no training run {tr_id}")
    return control.list_anomalies(tr_id)


@app.post("/training/{tr_id}/scan", status_code=202)
def scan_training(tr_id: str):
    """Manually run one monitor tick for this run (discover → fan out → reconcile). Normally the
    leader-elected monitor loop does this; exposed for dev/test and on-demand refresh."""
    if not control.get_training_run(tr_id):
        raise HTTPException(status_code=404, detail=f"no training run {tr_id}")
    training.discover(tr_id)
    fanned = training.fan_out(tr_id)
    evaluated = training.reconcile(tr_id)
    return {"fanned_out": fanned, "evaluated": evaluated}
