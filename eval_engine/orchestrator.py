"""Orchestrator entrypoint — admit → reconcile → finalize.

    python -m eval_engine.orchestrator

Runs leader-elected (a Postgres advisory lock; see ``main``), so >1 replica is safe — only the
leader ticks, and a standby takes over on handover or a reaped stale lock (bug B1). Each tick:
  - **Admit** (``_admit``): two-lane admission (docs/SCHEDULER.md §2) — ``queued`` runs become
    ``running`` under a global cap with a reserved interactive slice (the API already expanded the
    ledger in ``launch()``).
  - **Reconcile**: publish each running run's live rollup onto its runs row (DESIGN §8) and enforce
    its budget cap (skip still-queued samples once committed cost reaches the cap).
  - **Finalize**: a ``running`` run whose ledger is fully terminal (``done+failed+budget_skipped ==
    total``) gets a safety-sweep load, then aggregate → archive failures → prune ledger → mark
    ``completed`` (or ``budget_exceeded``).

The finalize gate keys on the run's authoritative ``total`` (set at create), so a run still being
expanded — where ``done+failed`` momentarily equals the partial count — is never finalized early.
Everything is idempotent: a crash mid-finalize just re-runs the (no-op-on-reentry) steps.
"""
from __future__ import annotations

import os
import time

from . import db, runner
from .logs import get_logger
from .models import RunSpec

log = get_logger(__name__)

TICK_SECONDS = float(os.environ.get("EVAL_ENGINE_ORCH_TICK", "2.0"))
LEADER_KEY = 0x6576616C  # 'eval' — the advisory-lock key so only one orchestrator ticks at a time
STALE_LEADER_SECONDS = float(os.environ.get("EVAL_ENGINE_STALE_LEADER_SECONDS", "20"))

# Two-lane admission (SCHEDULER §2): a global cap on concurrently-running runs + a reserved interactive
# slice that batch can borrow only when there's no interactive demand (and yields by attrition when
# there is). Per-run progress is then guaranteed by the per-run max_inflight cap at the claim (§3).
GLOBAL_MAX_RUNNING = int(os.environ.get("EVAL_ENGINE_GLOBAL_MAX_RUNNING", "50"))
INTERACTIVE_RESERVE = int(os.environ.get("EVAL_ENGINE_INTERACTIVE_RESERVE", "12"))  # ~25% of the global cap


def _admit() -> None:
    """Two-lane admission (SCHEDULER §2). Interactive runs may use any of the GLOBAL_MAX_RUNNING slots
    (including the reserve); batch runs are capped below the reserve whenever there's interactive
    demand (queued or running) — so a quick iteration run always gets slots, while running batch work
    is never preempted (yield by attrition at the admission boundary, not mid-run)."""
    running = db.control.lane_running_counts()
    n_running = running.get("interactive", 0) + running.get("batch", 0)
    queued = db.control.queued_runs_with_lane()  # FIFO by created_at
    if not queued:
        return
    interactive_demand = running.get("interactive", 0) > 0 or any(ln == "interactive" for _, ln in queued)
    batch_ceiling = GLOBAL_MAX_RUNNING - (INTERACTIVE_RESERVE if interactive_demand else 0)
    for run_id, lane in queued:
        if n_running >= GLOBAL_MAX_RUNNING:
            break  # global admission cap (blast radius + shared provider quota)
        if lane == "interactive" or n_running < batch_ceiling:
            db.control.set_status(run_id, "running")
            n_running += 1
            log.info("admitted %s (lane=%s) — now %d/%d running", run_id, lane, n_running, GLOBAL_MAX_RUNNING)


POD = os.environ.get("HOSTNAME", f"orch-{os.getpid()}")  # pod name in K8s → unique instance id


def tick() -> None:
    t0 = time.time()
    _admit()

    running_runs = db.control.active_runs(("running",))
    for run_id in running_runs:
        spec = RunSpec.model_validate_json(db.control.get_spec(run_id))
        # Live rollup (DESIGN §8 "Live metrics"): publish progress + live score + cost onto the runs
        # row each tick, so clients read live state from one authoritative place (no client-side agg).
        ld, lf, lp, lc = db.control.live_rollup(run_id)
        db.control.update_live(run_id, ld, lf, (lp / ld) if ld else 0.0, lc)
        # Budget cap (DESIGN §8): once committed cost reaches the cap, stop scheduling — convert
        # still-queued samples to the distinct terminal `budget_skipped` (in-flight ones finish).
        # Workers also enforce this (stop claiming early); the orchestrator is the authoritative sweep.
        skipped = runner.enforce_budget(run_id, spec)
        if skipped:
            log.warning("%s hit budget $%.6f → skipped %d queued (budget_exceeded)",
                        run_id, spec.budget_usd, skipped)
        total = db.control.run_total(run_id)
        c = db.control.counts(run_id)
        terminal = c.get("done", 0) + c.get("failed", 0) + c.get("budget_skipped", 0)
        log.debug("reconcile %s: %d/%d terminal (done=%d failed=%d skipped=%d queued=%d running=%d)",
                  run_id, terminal, total, c.get("done", 0), c.get("failed", 0),
                  c.get("budget_skipped", 0), c.get("queued", 0), c.get("running", 0))
        if total > 0 and terminal >= total and c.get("queued", 0) == 0 and c.get("running", 0) == 0:
            runner.batch_load(run_id, spec)  # safety sweep: ensure all done rows are in analytics
            done, failed, acc = runner.finalize(run_id, spec)
            log.info("finalized %s: done=%d failed=%d acc=%.3f", run_id, done, failed, acc)

    # Liveness for the ops dashboard: make the leader-elected singleton observable without the k8s
    # API (the standby reports leader=false from its loop below). detail carries this tick's signals.
    db.control.heartbeat("orchestrator", POD,
                         {"leader": True, "running_runs": len(running_runs),
                          "tick_ms": round((time.time() - t0) * 1000)})
    # Leader housekeeping: drop heartbeat rows from long-gone pods (KEDA churns many worker pod names)
    # so the table stays small. The cutoff is well above any single batch's wall-clock cap, so a busy
    # worker is never pruned; the snapshot already age-filters for liveness, this just bounds growth.
    db.control.prune_heartbeats(3600.0)


def main() -> None:
    db.init()
    # Leader election (shared loop): block as a standby until we hold the advisory lock, so running >1
    # orchestrator replica is safe (only the leader ticks). A standby takes over when the lock releases
    # — gracefully (SIGTERM handover) or, on an ungraceful death, by reaping the lingering idle lock
    # (bug B1). A tick error propagates → pod restart → re-contend (no swallow). The standby heartbeat
    # makes it observable; the leader heartbeats from inside tick().
    db.control.run_as_leader(
        LEADER_KEY, tick, tick_seconds=TICK_SECONDS, stale_seconds=STALE_LEADER_SECONDS,
        name="orchestrator",
        on_standby=lambda: db.control.heartbeat("orchestrator", POD, {"leader": False}))


if __name__ == "__main__":
    main()
