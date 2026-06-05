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
import time

from . import db, runner
from .models import RunSpec

TICK_SECONDS = float(os.environ.get("EVAL_ENGINE_ORCH_TICK", "2.0"))
LEADER_KEY = 0x6576616C  # 'eval' — the advisory-lock key so only one orchestrator ticks at a time


def tick() -> None:
    for run_id in db.control.active_runs(("queued",)):
        db.control.set_status(run_id, "running")
        print(f"[orch] admitted {run_id}", flush=True)

    for run_id in db.control.active_runs(("running",)):
        spec = RunSpec.model_validate_json(db.control.get_spec(run_id))
        # Budget cap (DESIGN §8): once committed cost reaches the cap, stop scheduling — convert
        # still-queued samples to the distinct terminal `budget_skipped` (in-flight ones finish).
        if spec.budget_usd and db.control.run_cost(run_id) >= spec.budget_usd:
            skipped = db.control.budget_stop(run_id)
            if skipped:
                print(f"[orch] {run_id} hit budget ${spec.budget_usd:.4f} → skipped {skipped} queued "
                      f"(budget_exceeded)", flush=True)
        total = db.control.run_total(run_id)
        c = db.control.counts(run_id)
        terminal = c.get("done", 0) + c.get("failed", 0) + c.get("budget_skipped", 0)
        if total > 0 and terminal >= total and c.get("queued", 0) == 0 and c.get("running", 0) == 0:
            runner._batch_load(run_id, spec)  # safety sweep: ensure all done rows are in analytics
            done, failed, acc = runner._finalize(run_id, spec)
            print(f"[orch] finalized {run_id}: done={done} failed={failed} acc={acc:.3f}", flush=True)


def main() -> None:
    db.init()
    # Leader election: block as a standby until we hold the advisory lock, so running >1 orchestrator
    # replica is safe (only the leader ticks). A standby takes over when the leader's lock releases.
    while not db.control.acquire_leader(LEADER_KEY):
        print("[orch] standby — another orchestrator holds leadership", flush=True)
        time.sleep(10)
    print(f"[orch] up, leader (backend={db.BACKEND})", flush=True)
    while True:
        if not db.control.leader_alive():
            print("[orch] lost leadership; exiting to re-contend", flush=True)
            return  # k8s restarts the pod → it re-enters as a standby
        tick()
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
