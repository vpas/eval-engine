"""Training monitor — continuous checkpoint evaluation (docs/TRAINING_MONITOR.md).

A thin, leader-elected loop (the orchestrator pattern) that watches an external — and, in v1, **mocked**
— trainer's checkpoint stream and drives the *existing* eval machinery against each checkpoint. Per
tick, for each active training run:

  discover  → read new checkpoints from the watched storage prefix (the poller; index.jsonl is the
              commit signal), record them, advance current_step.
  fan out   → for each new checkpoint launch the eval SUITE as ordinary tagged Runs (cadence + skip-
              stale policy), `model = checkpoint.model_ref` (resolved to a real model by §4).
  reconcile → when a checkpoint's suite runs are all terminal, roll their analytics summaries into the
              per-(eval,step) score series (with a Wilson CI), refit the expected curve, and run the
              §8 anomaly detectors → persist anomalies with a diagnosis.
  finalize  → when the trainer marks the run terminal, finish monitoring (best-checkpoint summary).

It governs fan-out, not safety: a crash just lets discovery lag a few seconds. Checkpoint-evals are
ordinary runs, so the ledger / KEDA workers / ClickHouse projection / Compare view all work unchanged;
in the cluster the orchestrator+workers execute the launched runs and the monitor reconciles their
results. ``EVAL_ENGINE_MONITOR_INLINE=1`` (dev/test) executes each launched run inline so one tick
fully processes a checkpoint without standing up separate orchestrator/worker processes.
"""
from __future__ import annotations

import json
import os

from . import runner, storage, training_analysis as ta
from .db import analytics, control
from .logs import get_logger
from .models import PluginRef, RunSpec, TrainingRunSpec

log = get_logger(__name__)

INLINE = os.environ.get("EVAL_ENGINE_MONITOR_INLINE", "0") == "1"
TICK_SECONDS = float(os.environ.get("EVAL_ENGINE_MONITOR_TICK", "3.0"))
LEADER_KEY = 0x6576616D  # 'evam' — distinct advisory-lock key from the orchestrator's
STALE_LEADER_SECONDS = float(os.environ.get("EVAL_ENGINE_STALE_LEADER_SECONDS", "20"))
# Default alert threshold (fraction): a regression must clear this AND the Wilson noise band (§8). The
# UI exposes it as a live pp knob; persisted anomalies store their magnitude so the threshold filters.
DEFAULT_THRESHOLD = float(os.environ.get("EVAL_ENGINE_ANOMALY_THRESHOLD_PP", "1.5")) / 100.0


# --------------------------------------------------------------------------- storage poller

class StoragePollingSource:
    """Reads the (mocked) trainer's manifests from a watched object-storage prefix via the fsspec
    abstraction (#14) — so the prefix is ``gs://…`` in-cluster and a local dir in dev with no code
    change. ``run.json`` is the TrainingRunSpec (+ a ``status``); ``index.jsonl`` is one
    CheckpointManifest per line (a new line = a checkpoint commit signal)."""

    @staticmethod
    def _join(source: str, name: str) -> str:
        return f"{source.rstrip('/')}/{name}"

    def read_run(self, source: str) -> dict | None:
        uri = self._join(source, "run.json")
        if not storage.exists(uri):
            return None
        return json.loads(storage.read_text(uri))

    def list_checkpoints(self, source: str) -> list[dict]:
        uri = self._join(source, "index.jsonl")
        if not storage.exists(uri):
            return []
        out = []
        for line in storage.read_text(uri).splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out


SOURCE = StoragePollingSource()


# --------------------------------------------------------------------------- registration

def register(spec: TrainingRunSpec) -> None:
    """Register a training run to monitor (idempotent on id)."""
    control.create_training_run(spec.model_dump())
    log.info("registered training run %s (model=%s, suite=%s)",
             spec.id, spec.model, [e.eval for e in spec.suite])


def register_from_source(source: str) -> TrainingRunSpec:
    """Read ``<source>/run.json`` and register the training run it describes."""
    raw = SOURCE.read_run(source)
    if raw is None:
        raise FileNotFoundError(f"no run.json under {source}")
    raw.setdefault("source", source)
    spec = TrainingRunSpec.model_validate(raw)
    register(spec)
    return spec


# --------------------------------------------------------------------------- discover

def discover(tr_id: str) -> list[dict]:
    """Poll the source; record any not-yet-seen checkpoints. Returns the newly-recorded ones. Also
    re-reads run.json to advance current_step and pick up a terminal trainer status."""
    tr = control.get_training_run(tr_id)
    if not tr:
        return []
    source = tr["source"]
    run_json = SOURCE.read_run(source) or {}
    seen = control.discovered_steps(tr_id)
    new: list[dict] = []
    for man in SOURCE.list_checkpoints(source):
        step = int(man["step"])
        if step in seen:
            continue
        ckpt = {
            "id": f"ck-{tr_id}-{step}", "training_run_id": tr_id, "step": step,
            "model_ref": man["model_ref"], "tokens": int(man.get("tokens", 0)),
            "wall_time": man.get("wall_time"), "train_metrics": man.get("train_metrics", {}),
        }
        if control.insert_checkpoint(ckpt):
            new.append(ckpt)
    # advance step + propagate a terminal trainer status (the monitor finalizes in reconcile)
    max_step = max([c["step"] for c in control.list_checkpoints(tr_id)] or [0])
    control.update_training_run(tr_id, current_step=max_step,
                                status="training" if tr["status"] == "watching" else None)
    if new:
        log.info("%s discovered %d new checkpoint(s): steps %s (current_step=%d)",
                 tr_id, len(new), [c["step"] for c in new], max_step)
    return new


# --------------------------------------------------------------------------- fan out

def _eval_runspec(entry: dict, model_ref: str, limit: int | None, cfg: dict) -> RunSpec:
    """Build the RunSpec for one suite eval against a checkpoint — resolve the registered eval's
    dataset (its pinned snapshot) + default harness/scorers, like the launch-from-eval path."""
    ev = control.get_entity("eval", entry["eval"], entry.get("version"))
    if not ev:
        raise KeyError(f"suite eval not registered: {entry['eval']}")
    e = ev["body"]
    ds = control.get_entity("dataset", e["dataset"])
    if not ds:
        raise KeyError(f"eval {entry['eval']} references unregistered dataset {e['dataset']}")
    dataset_uri = ds["body"].get("snapshot_uri") or ds["body"]["uri"]
    return RunSpec(
        eval=entry["eval"], eval_version=ev["version"], dataset=dataset_uri, model=model_ref,
        harness=PluginRef(**e["default_harness"]), scorers=[PluginRef(**s) for s in e["default_scorers"]],
        limit=limit, lane=cfg.get("lane", "interactive"), epochs=cfg.get("epochs", 1),
        budget_usd=cfg.get("budget_usd"),
    )


def fan_out(tr_id: str, inline: bool = INLINE) -> int:
    """Launch the eval suite for every newly-discovered checkpoint, applying the run's cadence
    (eval every Kth) and skip-stale (cap the un-evaluated backlog, keep the latest) policy. Returns
    the number of checkpoints fanned out."""
    tr = control.get_training_run(tr_id)
    if not tr:
        return 0
    body = tr["body"] or {}
    suite = body.get("suite", [])
    cfg = body.get("config", {}) or {}
    sweep = f"ckpt-{tr['model']}"

    all_ckpts = control.list_checkpoints(tr_id)            # sorted by step
    idx_of = {c["step"]: i for i, c in enumerate(all_ckpts)}  # discovery order = step order
    pending = [c for c in all_ckpts if c["status"] == "discovered"]

    # cadence: only every Kth discovered checkpoint is evaluated; the rest are skipped (gapped).
    eval_every = int(cfg.get("eval_every", 1) or 1)
    to_eval, skip = [], []
    for c in pending:
        (to_eval if ta.should_eval(idx_of[c["step"]], eval_every) else skip).append(c)

    # skip-stale safety valve: if the eval backlog is too deep, keep only the latest N, skip the rest.
    max_pending = cfg.get("max_pending_checkpoints")
    if max_pending and len(to_eval) > int(max_pending):
        keep = set(c["id"] for c in to_eval[-int(max_pending):])
        skip += [c for c in to_eval if c["id"] not in keep]
        to_eval = [c for c in to_eval if c["id"] in keep]

    for c in skip:
        control.set_checkpoint_status(c["id"], "skipped")
    if skip:
        log.info("%s skipping %d checkpoint(s) (cadence/skip-stale): steps %s",
                 tr_id, len(skip), [c["step"] for c in skip])

    n = 0
    for c in to_eval:
        launched = []
        for entry in suite:
            limit = ta.sample_limit_for(idx_of[c["step"]], cfg.get("sample_limit"),
                                        cfg.get("milestone_every"), entry.get("sample_limit"))
            spec = _eval_runspec(entry, c["model_ref"], limit, cfg)
            run_id = runner.launch(spec, created_by=tr.get("owner") or None, provenance={
                "training_run_id": tr_id, "checkpoint_id": c["id"], "step": c["step"], "sweep": sweep})
            control.audit(tr.get("owner"), "checkpoint.eval", run_id,
                          {"training_run": tr_id, "step": c["step"], "eval": entry["eval"]})
            launched.append((run_id, spec))
        control.set_checkpoint_status(c["id"], "evaluating")
        n += 1
        log.info("%s fanned out checkpoint %s (step %d) → %d suite run(s)%s",
                 tr_id, c["id"], c["step"], len(launched), " [inline]" if inline else "")
        if inline:  # dev/test: run each checkpoint-eval to completion now (cluster: workers do this)
            for run_id, spec in launched:
                runner.execute(run_id, spec)
    return n


# --------------------------------------------------------------------------- reconcile + detect

_TERMINAL = {"completed", "failed", "budget_exceeded", "cancelled"}


def reconcile(tr_id: str) -> int:
    """Roll completed checkpoint-eval runs into the per-(eval,step) score series, mark checkpoints
    evaluated, then refit + detect. Returns the number of checkpoints newly evaluated."""
    evaluated = 0
    for c in control.list_checkpoints(tr_id, status="evaluating"):
        runs = control.runs_for_checkpoint(c["id"])
        if not runs or any(r["status"] not in _TERMINAL for r in runs):
            continue  # still running — try again next tick
        for r in runs:
            n, passed, _mean, _tok, _cost = analytics.run_summary(r["run_id"])
            n, passed = int(n), int(passed)
            acc = (passed / n) if n else 0.0
            ci_lo, ci_hi = runner.wilson_ci(passed, n)
            control.upsert_checkpoint_score({
                "training_run_id": tr_id, "eval_id": r["eval_id"], "step": c["step"],
                "run_id": r["run_id"], "n": n, "passed": passed, "accuracy": acc,
                "ci_lo": ci_lo, "ci_hi": ci_hi, "sample_errors": int(r["failed"]),
                "expected": None,
            })
        control.set_checkpoint_status(c["id"], "evaluated")
        evaluated += 1
        log.info("%s checkpoint %s (step %d) evaluated → rolled %d eval score(s) into the series",
                 tr_id, c["id"], c["step"], len(runs))
    if evaluated:
        detect(tr_id)
        _maybe_finalize(tr_id)
    return evaluated


def _canary_ids(tr_id: str) -> set[str]:
    tr = control.get_training_run(tr_id) or {}
    return {e["eval"] for e in (tr.get("body") or {}).get("suite", []) if e.get("role") == "canary"}


def detect(tr_id: str, threshold: float = DEFAULT_THRESHOLD) -> list[dict]:
    """Refit the expected curve per eval, store it on the score rows, and run the CI-aware detectors;
    persist anomalies with their diagnosis. Returns the anomalies found this pass."""
    scores = control.checkpoint_scores(tr_id)
    by_eval: dict[str, list[dict]] = {}
    for s in scores:
        by_eval.setdefault(s["eval_id"], []).append(s)

    # breadth: how many evals are below their expected band at a given step (broad-vs-isolated, §8).
    expected_fns = {ev: ta.fit_expected([(r["step"], r["accuracy"]) for r in rows])
                    for ev, rows in by_eval.items()}
    below_at: dict[int, int] = {}
    for ev, rows in by_eval.items():
        f = expected_fns[ev]
        for r in rows:
            exp = f(r["step"])
            control.set_checkpoint_expected(tr_id, ev, r["step"], exp)
            r["expected"] = exp
            if exp - r["accuracy"] > threshold:
                below_at[r["step"]] = below_at.get(r["step"], 0) + 1

    canaries = _canary_ids(tr_id)
    ckpts = {c["step"]: c for c in control.list_checkpoints(tr_id)}
    found = []
    for ev, rows in by_eval.items():
        rows = sorted(rows, key=lambda r: r["step"])
        anom = ta.detect_eval_anomaly(rows, threshold)
        if not anom:
            continue
        a = _build_anomaly(tr_id, ev, anom, by_eval, ckpts, below_at, canaries, expected_fns, threshold)
        control.insert_anomaly(a)
        found.append(a)
        log.warning("%s ANOMALY eval=%s step=%d kind=%s Δ=%.3f severity=%s → diagnosis=%s (%s)",
                    tr_id, ev, a["step"], a["kind"], a["delta"], a["severity"], a["diagnosis"], a["cause"])
    return found


def _at(by_eval: dict, ev: str, step: int) -> dict | None:
    return next((r for r in by_eval.get(ev, []) if r["step"] == step), None)


def _build_anomaly(tr_id, ev, anom, by_eval, ckpts, below_at, canaries, expected_fns, threshold) -> dict:
    """Assemble the persisted anomaly: corroborating signals + diagnosis + category/regressed-sample
    drill data (§8). Separates a training/serving fault from a model merely weak on a hard eval."""
    step, from_step = anom["step"], anom["from"]
    cur = _at(by_eval, ev, step) or {}

    # training-metric cross-check (loss/grad/throughput/lr) from the checkpoint telemetry.
    cm = (ckpts.get(step) or {}).get("train_metrics", {}) or {}
    bm = (ckpts.get(from_step) or {}).get("train_metrics", {}) or {}
    loss_delta = (cm.get("loss") - bm.get("loss")) if (cm.get("loss") is not None and bm.get("loss") is not None) else None
    grad = cm.get("grad")
    tp_drop = ((bm.get("throughput") - cm.get("throughput")) / bm["throughput"]
               if cm.get("throughput") and bm.get("throughput") else None)
    lr_changed = (cm.get("lr") is not None and bm.get("lr") is not None
                  and abs(cm["lr"] - bm["lr"]) > 1e-9 and (cm["lr"] < bm["lr"] * 0.5))

    n = cur.get("n") or 0
    errs = cur.get("sample_errors", 0) or 0
    error_rate = errs / (n + errs) if (n + errs) else 0.0
    breadth = below_at.get(step, 1)
    # canary status: any canary eval collapsed at this step ⇒ model/serving broken (training fault).
    canary_collapsed = any(
        (r := _at(by_eval, cv, step)) is not None and expected_fns[cv](step) - r["accuracy"] > 0.25
        for cv in canaries if cv in expected_fns
    )
    # capability rising in parallel (the alignment-tax signal): a non-canary eval is still at/above its
    # expected band at this step while THIS eval erodes — the safety-vs-capability divergence.
    capability_rising = anom["kind"] == "drift" and any(
        ev2 != ev and ev2 not in canaries and (r := _at(by_eval, ev2, step)) is not None
        and r["accuracy"] >= expected_fns[ev2](step) - threshold
        for ev2 in by_eval
    )

    diagnosis, cause = ta.diagnose(
        anom["kind"], canary_collapsed=canary_collapsed, error_rate=error_rate, breadth=breadth,
        loss_delta=loss_delta, grad=grad, throughput_drop=tp_drop, capability_rising=capability_rising)
    signals = ta.build_signals(loss_delta=loss_delta, grad=grad, throughput_drop=tp_drop,
                               lr_changed=lr_changed, error_rate=error_rate, breadth=breadth)
    categories, samples = _drill(tr_id, ev, step, from_step)
    return {
        "id": f"an-{tr_id}-{ev}-{step}", "training_run_id": tr_id, "eval_id": ev, "step": step,
        "kind": anom["kind"], "severity": anom["severity"], "delta": anom["delta"],
        "from_step": from_step, "diagnosis": diagnosis, "cause": cause, "signals": signals,
        "categories": categories, "samples": samples,
    }


def _drill(tr_id: str, ev: str, step: int, from_step: int) -> tuple[list[dict], list[str]]:
    """Category regression (acc vs the baseline step) + regressed samples (passed before, fail now) —
    the anomaly drill data, computed from the two checkpoint-runs' analytics projections."""
    cur_run = control.run_for_step(tr_id, ev, step)
    base_run = control.run_for_step(tr_id, ev, from_step)
    categories: list[dict] = []
    samples: list[str] = []
    if cur_run and base_run:
        cur_cat = {gk: acc for gk, _n, _p, acc in analytics.by_category(cur_run)}
        base_cat = {gk: acc for gk, _n, _p, acc in analytics.by_category(base_run)}
        for gk in sorted(set(cur_cat) | set(base_cat)):
            categories.append({"cat": gk or "", "acc": cur_cat.get(gk, 0.0), "prev": base_cat.get(gk, 0.0)})
        # analytics.samples() yields (sample_id, passed, group_key, score, transcript_uri,
        # tokens, latency_ms, error_type); we only need id + pass/fail, so index rather than
        # unpack (robust to the projection's column set growing).
        cur_pass = {row[0]: row[1] for row in analytics.samples(cur_run)}
        for row in analytics.samples(base_run):
            sid, p = row[0], row[1]
            if p and not cur_pass.get(sid, 0):          # was pass → now fail
                samples.append(sid)
    return categories, samples[:25]


# --------------------------------------------------------------------------- finalize

def _maybe_finalize(tr_id: str) -> None:
    """If the trainer marked the run terminal and every discovered checkpoint is settled, finish
    monitoring."""
    tr = control.get_training_run(tr_id)
    if not tr:
        return
    run_json = SOURCE.read_run(tr["source"]) or {}
    trainer_status = run_json.get("status")
    if trainer_status not in ("completed", "failed"):
        return
    unsettled = [c for c in control.list_checkpoints(tr_id)
                 if c["status"] in ("discovered", "evaluating")]
    if unsettled:
        return
    control.update_training_run(tr_id, status=trainer_status, finished=True)
    log.info("%s monitoring finished (trainer status=%s); best checkpoints=%s",
             tr_id, trainer_status, best_checkpoints(tr_id))


def best_checkpoints(tr_id: str) -> dict[str, dict]:
    """Best checkpoint per eval (argmax accuracy) — the 'ship candidate' summary (§10)."""
    by_eval: dict[str, dict] = {}
    for s in control.checkpoint_scores(tr_id):
        b = by_eval.get(s["eval_id"])
        if b is None or (s["accuracy"] or 0) > (b["accuracy"] or 0):
            by_eval[s["eval_id"]] = {"step": s["step"], "accuracy": s["accuracy"], "run_id": s["run_id"]}
    return by_eval


# --------------------------------------------------------------------------- tick / loop

def tick(inline: bool = INLINE) -> None:
    for tr_id in control.active_training_runs():
        discover(tr_id)
        fan_out(tr_id, inline=inline)
        reconcile(tr_id)


def main() -> None:
    from . import db
    db.init()
    # Leader-elected (shared loop): only one monitor ticks; a standby takes over on handover/reap.
    # swallow_tick_errors=True — one bad training run must not kill the monitor loop.
    control.run_as_leader(LEADER_KEY, tick, tick_seconds=TICK_SECONDS,
                          stale_seconds=STALE_LEADER_SECONDS, name="monitor",
                          swallow_tick_errors=True)


if __name__ == "__main__":
    main()
