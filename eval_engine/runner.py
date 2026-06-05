"""Local runner — the production result path (ORCHESTRATION §4–§10) at single-process scale.

  create run → expand ledger → [claim batch → execute via Inspect → commit result →
  batch-load to analytics] loop → finalize (aggregate, archive failures, prune ledger).

Real Ray/Postgres swap only changes the concurrency primitive (claim) and the backends; the
lifecycle shape is exactly this.
"""
from __future__ import annotations

import datetime
import json
import os
import time
import urllib.request
from pathlib import Path

from inspect_ai import Task
from inspect_ai import eval as inspect_eval
from inspect_ai.dataset import MemoryDataset
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.scorer import CORRECT

from . import builtins, plugins  # noqa: F401  builtins import populates the registry
from .db import analytics, control
from .datasets import load_jsonl
from .models import RunSpec

TRANSCRIPTS = control.DATA / "transcripts"  # local object-store stand-in (dev)
GCS_BUCKET = os.environ.get("EVAL_ENGINE_GCS_BUCKET")  # set in-cluster → transcripts go to GCS
# Inspect's rich `.eval` logs go to GCS too (so the Inspect log viewer can read them); local in dev.
EVAL_LOG_DIR = f"gs://{GCS_BUCKET}/eval-logs" if GCS_BUCKET else str(control.DATA / "logs")
_gcs_client = None

# Per-sample retry policy (FR5, ORCHESTRATION §7). A transient sample failure re-queues with
# exponential `not_before` backoff up to MAX_ATTEMPTS, then goes terminal `failed`. The claim already
# increments attempts; backoff keeps a poison sample from head-of-line blocking the queue.
MAX_ATTEMPTS = int(os.environ.get("EVAL_ENGINE_MAX_ATTEMPTS", "3"))
RETRY_BASE_SECONDS = float(os.environ.get("EVAL_ENGINE_RETRY_BASE_SECONDS", "2.0"))
RETRY_CAP_SECONDS = float(os.environ.get("EVAL_ENGINE_RETRY_CAP_SECONDS", "60.0"))


def _settle_result(run_id: str, sid: str, result: dict | None) -> str:
    """Commit a clean result; retry-with-backoff a *transient* failure — a missing sample
    (``no_result``) or one Inspect recorded an execution error on. A merely low-scoring (wrong but
    error-free) sample is a clean result, not a failure. Returns ``'done'``/``'retry'``/``'failed'``."""
    err = "no_result" if result is None else result.get("error_type")
    if err:
        return control.retry_or_fail(
            run_id, sid, err, MAX_ATTEMPTS, RETRY_BASE_SECONDS, RETRY_CAP_SECONDS
        )
    control.commit_result(run_id, sid, result)
    return "done"


def _gcs():
    global _gcs_client
    if _gcs_client is None:
        from google.cloud import storage  # ADC = the GKE node SA (cloud-platform scope)
        _gcs_client = storage.Client()
    return _gcs_client


def _split_model(model: str) -> tuple[str, str]:
    return tuple(model.split("/", 1)) if "/" in model else ("", model)


def _score_value(value) -> float:
    if value in (CORRECT, "C", 1, 1.0, True):
        return 1.0
    return float(value) if isinstance(value, (int, float)) else 0.0


_OR_PRICES: dict[str, tuple[float, float]] | None = None  # model_id → ($/prompt_tok, $/completion_tok)


def _openrouter_prices() -> dict[str, tuple[float, float]]:
    """Fetch+cache OpenRouter's per-token catalog prices (once per process). Offline → empty."""
    global _OR_PRICES
    if _OR_PRICES is None:
        _OR_PRICES = {}
        try:
            with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=10) as r:
                for m in json.load(r)["data"]:
                    p = m.get("pricing", {})
                    _OR_PRICES[m["id"]] = (float(p.get("prompt") or 0), float(p.get("completion") or 0))
        except Exception:
            pass  # API down / no network → prices stay empty → cost 0.0 (graceful, never fatal)
    return _OR_PRICES


def _cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """Cost from tokens × OpenRouter catalog price. Prices both **direct** ``openrouter/<id>`` and
    **gateway** ``openai/<id>`` calls: in this deployment the LiteLLM gateway fronts OpenRouter at the
    same catalog price, so ``openai/<id>`` == ``openrouter/<id>`` in dollar terms (the gateway routes
    ``*`` → ``openrouter/*``). The production-canonical cost is the gateway's own per-``run_id`` tally
    (DESIGN §8 / A5); querying that at finalize is the deferred refinement — see docs/DEPLOYMENT.md."""
    prefix, _, mid = model.partition("/")
    if prefix not in ("openrouter", "openai") or not mid:
        return 0.0
    prompt, completion = _openrouter_prices().get(mid, (0.0, 0.0))
    return tokens_in * prompt + tokens_out * completion


def _model_for(spec: RunSpec, n: int):
    if spec.model.startswith("mockllm"):
        if spec.mock_tool_calls:  # scripted agentic mock: emit the tool-call sequence in order
            outs = [ModelOutput.for_tool_call(spec.model, tc["tool"], tc.get("args", {}))
                    for tc in spec.mock_tool_calls]
            return get_model(spec.model, custom_outputs=outs)
        out = spec.mock_output or "Paris"
        return get_model(spec.model, custom_outputs=[ModelOutput.from_content(spec.model, out) for _ in range(n + 2)])
    return get_model(spec.model)


def _put_transcript(run_id: str, sample_id: str, payload: dict) -> str:
    """Persist a transcript; return its URI. GCS (gs://…) in-cluster, local file in dev."""
    body = json.dumps(payload, ensure_ascii=False)
    if GCS_BUCKET:
        key = f"runs/{run_id}/transcripts/{sample_id}.json"
        _gcs().bucket(GCS_BUCKET).blob(key).upload_from_string(body, content_type="application/json")
        return f"gs://{GCS_BUCKET}/{key}"
    d = TRANSCRIPTS / run_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sample_id}.json"  # prod: .json.zst
    path.write_text(body)
    return str(path)


def get_transcript(uri: str) -> str | None:
    """Read back a transcript by URI (gs://<our-bucket>/… or a local path). For the dashboard drill-in."""
    if uri.startswith("gs://"):
        bucket, _, key = uri[len("gs://"):].partition("/")
        if not GCS_BUCKET or bucket != GCS_BUCKET:  # only ever serve our own bucket
            return None
        blob = _gcs().bucket(bucket).blob(key)
        return blob.download_as_text() if blob.exists() else None
    p = Path(uri)
    return p.read_text() if p.exists() else None


def _execute_batch(spec: RunSpec, run_id: str, samples_by_id: dict, ids: list[str]) -> dict[str, dict]:
    """Run Inspect on the claimed shard; return {sample_id: result dict}."""
    sub = MemoryDataset([samples_by_id[i] for i in ids])
    built, _ = plugins.build("harness", spec.harness.model_dump())
    # An agentic harness returns (solver, sandbox); simple harnesses return just a solver. The
    # sandbox flows into the Task → Inspect provisions one per sample (local Docker; prod = hardened
    # per-sample K8s pods, with a pooled snapshot-restore service later — docs/FUTURE.md §4).
    solver, sandbox = built if isinstance(built, tuple) else (built, None)
    scorers = [plugins.build("scorer", s.model_dump())[0] for s in spec.scorers]
    task = Task(dataset=sub, solver=solver, scorer=scorers, sandbox=sandbox)
    log = inspect_eval(
        task, model=_model_for(spec, len(ids)), display="none",
        log_dir=EVAL_LOG_DIR,  # GCS in-cluster (Inspect viewer reads these), local in dev
    )[0]

    eval_log_uri = getattr(log, "location", "") or ""  # the .eval log holding this shard's samples
    out: dict[str, dict] = {}
    for s in log.samples or []:
        sid = str(s.id)
        score_vals = {name: _score_value(sc.value) for name, sc in (s.scores or {}).items()}
        primary = next(iter(score_vals.values()), 0.0)
        usage = getattr(s.output, "usage", None) if s.output else None
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
        completion = s.output.completion if s.output else ""
        # Inspect records an execution error (model/tool/sandbox exception) on the sample as `.error`
        # — distinct from a low score. A non-empty error_type routes the sample to retry (`_settle_result`).
        err = getattr(s, "error", None)
        error_type = str(getattr(err, "message", err))[:200] if err else ""
        # Don't waste a transcript write on a to-be-retried sample; the retry writes its own.
        uri = "" if error_type else _put_transcript(
            run_id, sid,
            {"input": str(s.input), "output": completion, "target": str(s.target),
             "scores": score_vals, "eval_log_uri": eval_log_uri},  # for the Inspect viewer deep-link
        )
        out[sid] = {
            "passed": 1 if primary >= 0.5 else 0,
            "primary_score": primary,
            "scores": score_vals,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": _cost_usd(spec.model, tokens_in, tokens_out),  # prod: LiteLLM gateway
            "latency_ms": 0,
            "error_type": error_type,
            "transcript_uri": uri,
        }
    return out


def _batch_load(run_id: str, spec: RunSpec, ids: list[str] | None = None) -> None:
    """Flatten done-but-unloaded ledger rows → analytics (one batched insert).

    ``ids`` scopes the load to a specific shard: each distributed worker loads only the rows
    it just committed, so concurrent loaders never fetch+mark the same rows (no race). The
    single-process runner passes ``None`` and drains everything unloaded.
    """
    provider, model_id = _split_model(spec.model)
    fin = datetime.datetime.utcnow()
    rows = control.fetch_unloaded(run_id, ids)
    if not rows:
        return
    tuples, ids = [], []
    for (sid, gk, passed, prim, scores, tin, tout, cost, lat, err, uri, attempt) in rows:
        ids.append(sid)
        scores_json = scores if isinstance(scores, str) else json.dumps(scores)  # PG JSONB → dict
        tuples.append((
            run_id, sid, spec.eval, 1, provider, model_id, spec.harness.type, gk or "",
            passed, prim, scores_json, tin, tout, cost, lat, attempt, err or "", uri or "",
            "none", fin,
        ))
    analytics.insert(tuples)
    control.mark_loaded(run_id, ids)


def launch(spec: RunSpec, created_by: str | None = None) -> str:
    """CONTROL-PLANE action: create the run + expand the ledger (queued). Returns run_id.

    ``created_by`` is the authenticated user's email (from the OIDC proxy header), recorded for
    attribution; None when unauthenticated (e.g. local CLI/dev).
    """
    dataset, dataset_hash = load_jsonl(spec.dataset, spec.limit)
    samples_by_id = {str(s.id): s for s in dataset}
    provider, model_id = _split_model(spec.model)

    run_id = control.new_run_id()
    control.create_run({
        "id": run_id, "eval_id": spec.eval, "eval_version": 1, "model": spec.model,
        "provider": provider, "model_id": model_id, "harness": spec.harness.type,
        "scorers": [s.type for s in spec.scorers], "total": len(samples_by_id),
        "dataset_hash": dataset_hash,
        "spec_json": spec.model_dump_json(),  # so a separate worker/orchestrator can rehydrate it
        "created_by": created_by,             # authenticated email (OIDC proxy header), attribution
    })
    control.expand_tasks(
        run_id,
        [(sid, (s.metadata or {}).get("category", "")) for sid, s in samples_by_id.items()],
    )
    return run_id


def _finalize(run_id: str, spec: RunSpec) -> tuple[int, int, float]:
    """Aggregate the run, archive failures, prune the ledger, mark completed. Run ONCE per run
    (by the single-process runner, or by the distributed coordinator after all workers join)."""
    cnt = control.counts(run_id)
    done, failed = cnt.get("done", 0), cnt.get("failed", 0)
    budget_skipped = cnt.get("budget_skipped", 0)  # distinct terminal class (not a failure)
    n, passed, *_ = analytics.run_summary(run_id)
    accuracy = (passed / n) if n else 0.0
    status = "budget_exceeded" if budget_skipped else "completed"
    control.archive_and_prune(run_id)
    control.finalize_run(run_id, done, failed, accuracy, status=status)
    return done, failed, accuracy


def execute(run_id: str, spec: RunSpec) -> None:
    """ORCHESTRATOR action: claim → execute → commit → batch-load loop, then finalize."""
    dataset, _ = load_jsonl(spec.dataset, spec.limit)
    samples_by_id = {str(s.id): s for s in dataset}
    control.set_status(run_id, "running")

    worker = "w0"
    while True:
        ids = control.claim_batch(run_id, worker, spec.batch_size)
        if ids:
            results = _execute_batch(spec, run_id, samples_by_id, ids)
            for sid in ids:
                _settle_result(run_id, sid, results.get(sid))  # commit, or retry-with-backoff to N
            _batch_load(run_id, spec)
            if spec.budget_usd and control.run_cost(run_id) >= spec.budget_usd:
                control.budget_stop(run_id)  # cap reached → skip remaining queued (terminal, not failed)
            continue
        # Nothing claimable right now. If tasks remain (queued behind a not_before backoff, or
        # running), wait out the backoff and re-claim — don't finalize early. (The distributed path
        # relies on the orchestrator's finalize gate instead; this is the single-process equivalent.)
        c = control.counts(run_id)
        if c.get("queued", 0) == 0 and c.get("running", 0) == 0:
            break
        time.sleep(0.5)

    _finalize(run_id, spec)


def run(spec: RunSpec) -> str:
    """Synchronous launch+execute (used by the CLI)."""
    run_id = launch(spec)
    execute(run_id, spec)
    return run_id


__all__ = ["run", "launch", "execute", "RunSpec"]
