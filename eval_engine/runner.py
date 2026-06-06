"""Local runner — the production result path (ORCHESTRATION §4–§10) at single-process scale.

  create run → expand ledger → [claim batch → execute via Inspect → commit result →
  batch-load to analytics] loop → finalize (aggregate, archive failures, prune ledger).

The distributed deployment (KEDA-scaled worker Deployment + leader-elected orchestrator, all
coordinating through the Postgres ledger) only swaps the concurrency primitive (a shared claim
instead of this single ``w0`` loop); the lifecycle shape is exactly this.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import time
import urllib.request

import zstandard

from inspect_ai import Task
from inspect_ai import eval as inspect_eval
from inspect_ai.dataset import MemoryDataset
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.scorer import CORRECT

from . import builtins, plugins, storage  # noqa: F401  builtins import populates the registry
from .db import analytics, control
from .datasets import load_jsonl
from .models import RunSpec

TRANSCRIPTS = control.DATA / "transcripts"  # local object-store stand-in (dev)
GCS_BUCKET = os.environ.get("EVAL_ENGINE_GCS_BUCKET")  # set in-cluster → transcripts go to GCS
# Inspect's rich `.eval` logs go to GCS too (so the Inspect log viewer can read them); local in dev.
EVAL_LOG_DIR = f"gs://{GCS_BUCKET}/eval-logs" if GCS_BUCKET else str(control.DATA / "logs")

# Reproducibility pin (DESIGN §14): the worker image/code ref, baked at build time (Dockerfile ARG
# GIT_SHA → this env) and recorded on every run so a run's inputs include the exact code that ran it.
IMAGE_DIGEST = os.environ.get("EVAL_ENGINE_IMAGE_DIGEST", "dev")

# Two-lane admission (SCHEDULER §2): classify a run interactive vs batch at launch and pin its per-run
# concurrency cap (max_inflight). Interactive = small subset/iteration runs (a `limit`, or ≤ N samples)
# → small max_inflight so MANY iterators progress at once; batch = full runs → larger max_inflight.
INTERACTIVE_MAX_SAMPLES = int(os.environ.get("EVAL_ENGINE_INTERACTIVE_MAX_SAMPLES", "200"))
INTERACTIVE_MAX_INFLIGHT = int(os.environ.get("EVAL_ENGINE_INTERACTIVE_MAX_INFLIGHT", "5"))
BATCH_MAX_INFLIGHT = int(os.environ.get("EVAL_ENGINE_BATCH_MAX_INFLIGHT", "50"))


def _classify(spec: RunSpec, total: int) -> tuple[str, int]:
    """Return (lane, max_inflight) for a run (SCHEDULER §2/§3)."""
    if spec.lane in ("interactive", "batch"):
        lane = spec.lane
    else:
        lane = "interactive" if (spec.limit is not None or total <= INTERACTIVE_MAX_SAMPLES) else "batch"
    return lane, (INTERACTIVE_MAX_INFLIGHT if lane == "interactive" else BATCH_MAX_INFLIGHT)


# Per-sample retry policy (FR5, ORCHESTRATION §7). A transient sample failure re-queues with
# exponential `not_before` backoff up to MAX_ATTEMPTS, then goes terminal `failed`. The claim already
# increments attempts; backoff keeps a poison sample from head-of-line blocking the queue.
MAX_ATTEMPTS = int(os.environ.get("EVAL_ENGINE_MAX_ATTEMPTS", "3"))
RETRY_BASE_SECONDS = float(os.environ.get("EVAL_ENGINE_RETRY_BASE_SECONDS", "2.0"))
RETRY_CAP_SECONDS = float(os.environ.get("EVAL_ENGINE_RETRY_CAP_SECONDS", "60.0"))

# Hung-call protection. A model request that never returns must not wedge the (single-threaded) worker —
# it would hold its claimed tasks' lease in-process forever and starve every other run. MODEL_TIMEOUT
# bounds a single provider/gateway request (GenerateConfig.timeout); SAMPLE_TIME_LIMIT is a per-sample
# wall-clock cap (Inspect `time_limit`). A breach surfaces as a per-sample error → routed to
# retry-with-backoff; fail_on_error=False keeps the rest of the batch (and the worker) moving. Env-tunable.
MODEL_TIMEOUT = int(os.environ.get("EVAL_ENGINE_MODEL_TIMEOUT", "120"))
SAMPLE_TIME_LIMIT = int(os.environ.get("EVAL_ENGINE_SAMPLE_TIME_LIMIT", "600"))


def _enforce_budget(run_id: str, spec: RunSpec) -> int:
    """If the run has a budget and committed cost has reached it, skip the still-queued samples
    (terminal ``budget_skipped``, DESIGN §8). Returns # skipped. Idempotent — safe to call from the
    worker (stop claiming early) *and* the orchestrator (authoritative sweep) without double-counting."""
    if spec.budget_usd and control.run_cost(run_id) >= spec.budget_usd:
        return control.budget_stop(run_id)
    return 0


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


def _split_model(model: str) -> tuple[str, str]:
    return tuple(model.split("/", 1)) if "/" in model else ("", model)


def wilson_ci(passed: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a pass-rate proportion (FR8, DESIGN §14). Better than the normal
    approximation at small n / extreme rates (and never escapes [0,1]). z=1.96 → 95%. Returns
    (low, high); a zero-sample run is (0, 0)."""
    import math
    if n <= 0:
        return (0.0, 0.0)
    p = passed / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


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


def _resolve_model(spec: RunSpec) -> tuple[str, str | None]:
    """Resolve the model actually CALLED for inference. For a training-checkpoint run the spec's model
    is an opaque ``checkpoint:<tr>:<step>`` handle (docs/TRAINING_MONITOR.md §4); a mock resolver maps
    it to a real OpenRouter/mock model behind the scenes, so the rest of the engine treats the
    checkpoint as 'served by trainer infra' without knowing it's faked. In production the gateway would
    resolve a ``checkpoint:`` alias natively. Non-checkpoint models pass through unchanged. Returns
    (exec_model, mock_output_override)."""
    if spec.model.startswith("checkpoint:"):
        m = control.get_checkpoint_model(spec.model)
        if m:
            return m["real_model"], (m.get("mock_output") or spec.mock_output)
    return spec.model, spec.mock_output


def _build_model(model: str, mock_output: str | None, mock_tool_calls, n: int):
    if model.startswith("mockllm"):
        if mock_tool_calls:  # scripted agentic mock: emit the tool-call sequence in order
            outs = [ModelOutput.for_tool_call(model, tc["tool"], tc.get("args", {}))
                    for tc in mock_tool_calls]
            return get_model(model, custom_outputs=outs)
        out = mock_output or "Paris"
        return get_model(model, custom_outputs=[ModelOutput.from_content(model, out) for _ in range(n + 2)])
    return get_model(model)


def _model_for(spec: RunSpec, n: int):
    exec_model, mock_output = _resolve_model(spec)
    return _build_model(exec_model, mock_output, spec.mock_tool_calls, n)


def _commit_batch(spec: RunSpec, run_id: str, samples_by_id: dict, ids: list[str],
                  results: dict[str, dict]) -> None:
    """Ack-before-flip commit (DESIGN §8, ORCHESTRATION §5). For each clean result: durably insert to
    analytics FIRST, **then** flip its ledger row to ``done`` (+``loaded``). Invariant: ``done`` ⟹ the
    result is durable in ClickHouse, so a crash never leaves a ``done`` row missing from analytics — a
    row that crashes *after* the insert but *before* the flip stays ``running``, is re-claimed, and the
    re-insert (higher ``attempt`` = newer ReplacingMergeTree version) wins. Missing/errored samples
    route to retry-with-backoff; budget is enforced after."""
    good = {sid: r for sid in ids
            if (r := results.get(sid)) is not None and not r.get("error_type")}
    if good:
        attempts = control.attempts_for(run_id, list(good))  # ledger version for ReplacingMergeTree
        provider, model_id = _split_model(spec.model)
        fin = datetime.datetime.utcnow()
        tuples = []
        for sid, r in good.items():
            gk = (samples_by_id[sid].metadata or {}).get("category", "") if sid in samples_by_id else ""
            tuples.append((
                run_id, sid, spec.eval, spec.eval_version, provider, model_id, spec.harness.type, gk or "",
                r["passed"], r["primary_score"], json.dumps(r["scores"]), r["tokens_in"],
                r["tokens_out"], r["cost_usd"], r["latency_ms"], attempts.get(sid, 1),
                r["error_type"] or "", r["transcript_uri"] or "", "none", fin,
            ))
        analytics.insert(tuples)                        # (1) DURABLE insert — happens BEFORE the flip
        for sid, r in good.items():
            control.commit_result(run_id, sid, r)       # (2) now flip the ledger row to 'done'
        control.mark_loaded(run_id, list(good))         #     done ⟹ loaded ⟹ durable in analytics
        fp = next((r.get("provider_fingerprint") for r in good.values() if r.get("provider_fingerprint")), "")
        if fp:
            control.set_fingerprint(run_id, fp)         # repro pin: first fingerprint the run sees
    for sid in ids:
        if sid not in good:
            _settle_result(run_id, sid, results.get(sid))  # missing/errored → retry-with-backoff
    _enforce_budget(run_id, spec)


# Transcript retention (DESIGN §8/§13). Default sampling rate when a RunSpec doesn't set one — unset
# ⇒ keep all (dev/tests); the cluster sets e.g. 0.2 to sample-by-default. Failures are always kept.
TRANSCRIPT_SAMPLE_RATE = (lambda v: float(v) if v else None)(os.environ.get("EVAL_ENGINE_TRANSCRIPT_SAMPLE_RATE"))


def _hash01(s: str) -> float:
    """Deterministic [0,1) hash of a sample id — stable sampling (the same sample is always in/out)."""
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16) / 0x100000000


def _keep_transcript(spec: RunSpec, sample_id: str, passed: int) -> bool:
    """Retention decision (DESIGN §13): always keep failures; keep a deterministic fraction of passes."""
    rate = spec.transcript_sample_rate if spec.transcript_sample_rate is not None else TRANSCRIPT_SAMPLE_RATE
    if rate is None or rate >= 1.0:
        return True
    return passed == 0 or _hash01(sample_id) < rate


def _put_transcript(run_id: str, sample_id: str, payload: dict) -> str:
    """Persist a zstd-compressed transcript; return its URI. GCS (gs://…) in-cluster, local in dev."""
    body = zstandard.ZstdCompressor().compress(json.dumps(payload, ensure_ascii=False).encode())
    uri = (f"gs://{GCS_BUCKET}/runs/{run_id}/transcripts/{sample_id}.json.zst" if GCS_BUCKET
           else str(TRANSCRIPTS / run_id / f"{sample_id}.json.zst"))
    storage.write_bytes(uri, body)
    return uri


def get_transcript(uri: str) -> str | None:
    """Read back a transcript by URI (gs://<our-bucket>/… or a local path). For the dashboard drill-in.
    Transparently decompresses ``.zst`` (current format); plain ``.json`` (legacy) is returned as-is."""
    if uri.startswith("gs://"):
        bucket = uri[len("gs://"):].partition("/")[0]
        if not GCS_BUCKET or bucket != GCS_BUCKET:  # only ever serve our own bucket
            return None
    if not storage.exists(uri):
        return None
    raw = storage.read_bytes(uri)
    if uri.endswith(".zst"):
        raw = zstandard.ZstdDecompressor().decompress(raw)
    return raw.decode()


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
    # Sampling (DESIGN §14): epochs repeat each sample (Inspect reduces to one per-sample score);
    # temperature/seed go into the GenerateConfig that `eval` builds from **kwargs.
    epochs = spec.epochs if spec.epochs and spec.epochs > 1 else None
    gen: dict = {"timeout": MODEL_TIMEOUT}  # bound a single model request so a hung call can't wedge us
    if spec.temperature is not None:
        gen["temperature"] = spec.temperature
    if spec.seed is not None:
        gen["seed"] = spec.seed
    # Resolve the model actually called (a checkpoint ref → real model; §4) — used for both the
    # Inspect model object and for pricing (so cost reflects the real provider, not the opaque ref).
    exec_model, mock_output = _resolve_model(spec)
    model = _build_model(exec_model, mock_output, spec.mock_tool_calls, len(ids) * max(1, spec.epochs))
    log = inspect_eval(
        task, model=model, display="none",
        log_dir=EVAL_LOG_DIR,  # GCS in-cluster (Inspect viewer reads these), local in dev
        epochs=epochs, time_limit=SAMPLE_TIME_LIMIT, fail_on_error=False, **gen,
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
        # Provider version fingerprint (DESIGN §14): the resolved model the provider echoes back, plus
        # its system_fingerprint when exposed (some openai/groq models do; mock/OpenRouter often don't).
        resolved = (getattr(s.output, "model", "") or "") if s.output else ""
        sysfp = (getattr(s.output, "metadata", None) or {}).get("system_fingerprint") if s.output else None
        provider_fp = f"{resolved}@{sysfp}" if sysfp else resolved
        # Inspect records an execution error (model/tool/sandbox exception) on the sample as `.error`
        # — distinct from a low score. A non-empty error_type routes the sample to retry (`_settle_result`).
        err = getattr(s, "error", None)
        error_type = str(getattr(err, "message", err))[:200] if err else ""
        passed = 1 if primary >= 0.5 else 0
        # No transcript for a to-be-retried sample (the retry writes its own); otherwise keep it per
        # the retention policy (always failures, a sampled fraction of passes — DESIGN §13).
        uri = _put_transcript(
            run_id, sid,
            {"input": str(s.input), "output": completion, "target": str(s.target),
             "scores": score_vals, "eval_log_uri": eval_log_uri},  # for the Inspect viewer deep-link
        ) if (not error_type and _keep_transcript(spec, sid, passed)) else ""
        out[sid] = {
            "passed": passed,
            "primary_score": primary,
            "scores": score_vals,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": _cost_usd(exec_model, tokens_in, tokens_out),  # prod: LiteLLM gateway
            "latency_ms": 0,
            "error_type": error_type,
            "transcript_uri": uri,
            "provider_fingerprint": provider_fp,
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
            run_id, sid, spec.eval, spec.eval_version, provider, model_id, spec.harness.type, gk or "",
            passed, prim, scores_json, tin, tout, cost, lat, attempt, err or "", uri or "",
            "none", fin,
        ))
    analytics.insert(tuples)
    control.mark_loaded(run_id, ids)


def launch(spec: RunSpec, created_by: str | None = None, provenance: dict | None = None) -> str:
    """CONTROL-PLANE action: create the run + expand the ledger (queued). Returns run_id.

    ``created_by`` is the authenticated user's email (from the OIDC proxy header), recorded for
    attribution; None when unauthenticated (e.g. local CLI/dev). ``provenance`` optionally tags the run
    as a training checkpoint-eval (``training_run_id`` / ``checkpoint_id`` / ``step`` / ``sweep``,
    docs/TRAINING_MONITOR.md §2) — NULL/absent for an ordinary ad-hoc run.
    """
    dataset, dataset_hash = load_jsonl(spec.dataset, spec.limit)
    samples_by_id = {str(s.id): s for s in dataset}
    provider, model_id = _split_model(spec.model)
    prov = provenance or {}

    run_id = control.new_run_id()
    lane, max_inflight = _classify(spec, len(samples_by_id))
    control.create_run({
        "id": run_id, "eval_id": spec.eval, "eval_version": spec.eval_version, "model": spec.model,
        "provider": provider, "model_id": model_id, "harness": spec.harness.type,
        "scorers": [s.type for s in spec.scorers], "total": len(samples_by_id),
        "dataset_hash": dataset_hash,
        "spec_json": spec.model_dump_json(),  # so a separate worker/orchestrator can rehydrate it
        "created_by": created_by,             # authenticated email (OIDC proxy header), attribution
        "team": spec.team,                    # ownership (tenancy-ready; enforcement deferred)
        "image_digest": IMAGE_DIGEST,         # repro pin: the worker code/image that ran this (§14)
        "lane": lane, "max_inflight": max_inflight,  # admission lane + per-run cap (SCHEDULER §2/§3)
        "training_run_id": prov.get("training_run_id"), "checkpoint_id": prov.get("checkpoint_id"),
        "step": prov.get("step"), "sweep": prov.get("sweep"),  # checkpoint-eval provenance (§2)
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
    n, passed, _mean, _tokens, cost = analytics.run_summary(run_id)
    accuracy = (passed / n) if n else 0.0
    status = "budget_exceeded" if budget_skipped else "completed"
    control.archive_and_prune(run_id)
    control.finalize_run(run_id, done, failed, accuracy, cost_usd=cost, status=status)
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
            # ack-before-flip commit: durable analytics insert → flip ledger 'done'; + retry + budget
            _commit_batch(spec, run_id, samples_by_id, ids, results)
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
