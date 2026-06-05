"""Orchestrator entrypoint — admit → reconcile → finalize. One replica (leader-election later).

    python -m eval_engine.orchestrator

Each tick:
  - **Admit**: expanded ``queued`` runs become ``running`` (the API already expanded the ledger in
    ``launch()``). Two-lane admission (docs/SCHEDULER.md) is a future refinement — v1 admits all.
  - **Finalize**: a ``running`` run whose ledger is fully terminal (``done+failed == total``) gets a
    safety-sweep load, then aggregate → archive failures → prune ledger → mark ``completed``.

The finalize gate keys on the run's authoritative ``total`` (set at create), so a run still being
expanded — where ``done+failed`` momentarily equals the partial count — is never finalized early.
Everything is idempotent: a crash mid-finalize just re-runs the (no-op-on-reentry) steps.
"""
from __future__ import annotations

import os
import signal
import sys
import time

from . import db, runner
from .models import RunSpec

TICK_SECONDS = float(os.environ.get("EVAL_ENGINE_ORCH_TICK", "2.0"))
LEADER_KEY = 0x6576616C  # 'eval' — the advisory-lock key so only one orchestrator ticks at a time
STALE_LEADER_SECONDS = float(os.environ.get("EVAL_ENGINE_STALE_LEADER_SECONDS", "20"))


def tick() -> None:
    for run_id in db.control.active_runs(("queued",)):
        db.control.set_status(run_id, "running")
        print(f"[orch] admitted {run_id}", flush=True)

    for run_id in db.control.active_runs(("running",)):
        spec = RunSpec.model_validate_json(db.control.get_spec(run_id))
        # Live rollup (DESIGN §8 "Live metrics"): publish progress + live score + cost onto the runs
        # row each tick, so clients read live state from one authoritative place (no client-side agg).
        ld, lf, lp, lc = db.control.live_rollup(run_id)
        db.control.update_live(run_id, ld, lf, (lp / ld) if ld else 0.0, lc)
        # Budget cap (DESIGN §8): once committed cost reaches the cap, stop scheduling — convert
        # still-queued samples to the distinct terminal `budget_skipped` (in-flight ones finish).
        # Workers also enforce this (stop claiming early); the orchestrator is the authoritative sweep.
        skipped = runner._enforce_budget(run_id, spec)
        if skipped:
            print(f"[orch] {run_id} hit budget ${spec.budget_usd:.6f} → skipped {skipped} queued "
                  f"(budget_exceeded)", flush=True)
        total = db.control.run_total(run_id)
        c = db.control.counts(run_id)
        terminal = c.get("done", 0) + c.get("failed", 0) + c.get("budget_skipped", 0)
        if total > 0 and terminal >= total and c.get("queued", 0) == 0 and c.get("running", 0) == 0:
            runner._batch_load(run_id, spec)  # safety sweep: ensure all done rows are in analytics
            done, failed, acc = runner._finalize(run_id, spec)
            print(f"[orch] finalized {run_id}: done={done} failed={failed} acc={acc:.3f}", flush=True)


def _graceful_shutdown(*_) -> None:
    """SIGTERM (k8s pod delete / rollout): release the leader lock so the next pod takes over in ~1s
    instead of stalling until our pooled connection times out (bug B1)."""
    print("[orch] SIGTERM — releasing leadership", flush=True)
    db.control.release_leader(LEADER_KEY)
    sys.exit(0)


def main() -> None:
    db.init()
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    # Leader election: block as a standby until we hold the advisory lock, so running >1 orchestrator
    # replica is safe (only the leader ticks). A standby takes over when the leader's lock releases —
    # either gracefully (SIGTERM handover) or, if the old leader died ungracefully, by reaping its
    # lingering pooled connection once it's been idle past the threshold (bug B1).
    while not db.control.acquire_leader(LEADER_KEY):
        if db.control.reap_stale_leader(LEADER_KEY, STALE_LEADER_SECONDS):
            print("[orch] reaped a stale leader (lingering lock) — retrying for leadership", flush=True)
            continue
        print("[orch] standby — another orchestrator holds leadership", flush=True)
        time.sleep(5)
    print(f"[orch] up, leader (backend={db.BACKEND})", flush=True)
    while True:
        if not db.control.leader_alive():
            print("[orch] lost leadership; exiting to re-contend", flush=True)
            return  # k8s restarts the pod → it re-enters as a standby
        tick()
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
