"""Worker entrypoint — claim → execute → commit → load over the shared ledger.

Run N copies (a K8s Deployment, KEDA-scaled). The Postgres ledger (``FOR UPDATE SKIP LOCKED``) is
the sole coordinator, so a worker is just a copy of the loop — no inter-worker coordination. Crash
safety is the lease: a dead worker's claimed tasks are reclaimed by survivors once it expires.

    python -m eval_engine.worker          # connects to Postgres via EVAL_ENGINE_PG_DSN
"""
from __future__ import annotations

import os
import signal
import time

from . import db, runner
from .datasets import load_jsonl
from .models import RunSpec

POLL_SECONDS = float(os.environ.get("EVAL_ENGINE_WORKER_POLL", "1.0"))
WORKER_ID = os.environ.get("HOSTNAME", f"w-{os.getpid()}")  # pod name in K8s → unique claimer id

# Graceful drain on rollout/scale-down. K8s sends SIGTERM before SIGKILL; on SIGTERM we stop claiming
# NEW batches and let the in-flight one finish, then exit — so a rolling update never claims-then-dies
# (which would strand tasks `running` until lease expiry and bump their attempts on reclaim). The
# in-flight batch is bounded by the per-sample timeout (runner.MODEL_TIMEOUT/SAMPLE_TIME_LIMIT); set the
# Deployment's terminationGracePeriodSeconds above that so the batch completes before SIGKILL.
_STOP = False


def _graceful_shutdown(*_) -> None:
    global _STOP
    _STOP = True
    print(f"[worker {WORKER_ID}] SIGTERM — draining: finishing current batch, no new claims", flush=True)


def _drain_run(run_id: str) -> int:
    """Claim + execute this run's claimable tasks until none remain for us; return # processed."""
    spec_json = db.control.get_spec(run_id)
    if not spec_json:
        return 0
    spec = RunSpec.model_validate_json(spec_json)
    dataset, _ = load_jsonl(spec.dataset, spec.limit)
    samples_by_id = {str(s.id): s for s in dataset}
    processed = 0
    while True:
        if _STOP:                       # SIGTERM: stop claiming new work, let the loop unwind + exit
            return processed
        ids = db.control.claim_batch(run_id, WORKER_ID, spec.batch_size)
        if not ids:
            return processed
        # run_id in the log line so the ops dashboard's per-run "worker logs" deep link matches.
        print(f"[worker {WORKER_ID}] run_id={run_id} claimed {len(ids)}", flush=True)
        results = runner._execute_batch(spec, run_id, samples_by_id, ids)
        # ack-before-flip commit: durable analytics insert → flip ledger 'done'; + retry + budget
        runner._commit_batch(spec, run_id, samples_by_id, ids, results)
        processed += len(ids)
        # Refresh liveness between batches so a long multi-batch drain doesn't read as a dead worker.
        db.control.heartbeat("worker", WORKER_ID, {"claimed_this_loop": len(ids), "run": run_id})


def main() -> None:
    db.init()
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    print(f"[worker {WORKER_ID}] up", flush=True)
    while True:
        if _STOP:
            print(f"[worker {WORKER_ID}] drained — exiting", flush=True)
            return
        # Liveness for the ops dashboard (portable, no k8s API). Written at the TOP of the loop too —
        # not just after draining — so a worker registers the moment it's up and refreshes before each
        # drain attempt (a worker blocked in a long model call mid-batch still has this fresh-ish row;
        # the snapshot also reconciles against K8s pod readiness for the truly-busy case).
        db.control.heartbeat("worker", WORKER_ID, {"claimed_this_loop": 0})
        did = 0
        for run_id in db.control.active_runs(("running",)):
            if _STOP:
                break
            did += _drain_run(run_id)
        db.control.heartbeat("worker", WORKER_ID, {"claimed_this_loop": did})
        if did == 0:
            time.sleep(POLL_SECONDS)  # nothing claimable; let KEDA scale us down when idle


if __name__ == "__main__":
    main()
