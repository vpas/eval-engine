"""Local runner — the production result path (ORCHESTRATION §4–§10) at single-process scale.

  create run → expand ledger → [claim batch → execute via Inspect → commit result →
  batch-load to analytics] loop → finalize (aggregate, archive failures, prune ledger).

Real Ray/Postgres swap only changes the concurrency primitive (claim) and the backends; the
lifecycle shape is exactly this.
"""
from __future__ import annotations

import datetime
import json
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

TRANSCRIPTS = control.DATA / "transcripts"  # object-store stand-in (prod: S3 + zstd)


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
    d = TRANSCRIPTS / run_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sample_id}.json"  # prod: .json.zst
    path.write_text(json.dumps(payload, ensure_ascii=False))
    return str(path)


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
        log_dir=str(control.DATA / "logs"),
    )[0]

    out: dict[str, dict] = {}
    for s in log.samples or []:
        sid = str(s.id)
        score_vals = {name: _score_value(sc.value) for name, sc in (s.scores or {}).items()}
        primary = next(iter(score_vals.values()), 0.0)
        usage = getattr(s.output, "usage", None) if s.output else None
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
        completion = s.output.completion if s.output else ""
        uri = _put_transcript(
            run_id, sid,
            {"input": str(s.input), "output": completion, "target": str(s.target),
             "scores": score_vals},
        )
        out[sid] = {
            "passed": 1 if primary >= 0.5 else 0,
            "primary_score": primary,
            "scores": score_vals,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": _cost_usd(spec.model, tokens_in, tokens_out),  # prod: LiteLLM gateway
            "latency_ms": 0,
            "error_type": "",
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
    n, passed, *_ = analytics.run_summary(run_id)
    accuracy = (passed / n) if n else 0.0
    control.archive_and_prune(run_id)
    control.finalize_run(run_id, done, failed, accuracy)
    return done, failed, accuracy


def execute(run_id: str, spec: RunSpec) -> None:
    """ORCHESTRATOR action: claim → execute → commit → batch-load loop, then finalize."""
    dataset, _ = load_jsonl(spec.dataset, spec.limit)
    samples_by_id = {str(s.id): s for s in dataset}
    control.set_status(run_id, "running")

    worker = "w0"
    while ids := control.claim_batch(run_id, worker, spec.batch_size):
        results = _execute_batch(spec, run_id, samples_by_id, ids)
        for sid in ids:
            if sid in results:
                control.commit_result(run_id, sid, results[sid])
            else:
                control.mark_failed(run_id, sid, "no_result")
        _batch_load(run_id, spec)

    _finalize(run_id, spec)


def run(spec: RunSpec) -> str:
    """Synchronous launch+execute (used by the CLI)."""
    run_id = launch(spec)
    execute(run_id, spec)
    return run_id


__all__ = ["run", "launch", "execute", "RunSpec"]
