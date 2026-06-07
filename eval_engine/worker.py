"""Worker entrypoint — claim → execute → commit → load over the shared ledger.

Run N copies (a K8s Deployment, KEDA-scaled). The Postgres ledger (``FOR UPDATE SKIP LOCKED``) is
the sole coordinator, so a worker is just a copy of the loop — no inter-worker coordination. Crash
safety is the lease: a dead worker's claimed tasks are reclaimed by survivors once it expires.

    python -m eval_engine.worker          # connects to Postgres via EVAL_ENGINE_PG_DSN
"""
from __future__ import annotations

import os
import signal
import threading
import time

from . import db, runner
from .datasets import load_jsonl
from .logs import get_logger
from .models import RunSpec

log = get_logger(__name__)

POLL_SECONDS = float(os.environ.get("EVAL_ENGINE_WORKER_POLL", "1.0"))
WORKER_ID = os.environ.get("HOSTNAME", f"w-{os.getpid()}")  # pod name in K8s → unique claimer id
# Lease heartbeat: renew our claimed tasks' leases this often while executing, so a long batch
# (agentic / SWE-bench — image pull + multi-turn agent + test run, easily > the 600s claim lease)
# isn't reclaimed + redone by another worker. Well under the lease so a missed beat is harmless.
LEASE_RENEW_SECONDS = float(os.environ.get("EVAL_ENGINE_LEASE_RENEW_SECONDS", "120"))

# Graceful drain on rollout/scale-down. K8s sends SIGTERM before SIGKILL; on SIGTERM we stop claiming
# NEW batches and let the in-flight one finish, then exit — so a rolling update never claims-then-dies
# (which would strand tasks `running` until lease expiry and bump their attempts on reclaim). The
# in-flight batch is bounded by the per-sample timeout (runner.MODEL_TIMEOUT/SAMPLE_TIME_LIMIT); set the
# Deployment's terminationGracePeriodSeconds above that so the batch completes before SIGKILL.
_STOP = False


def _graceful_shutdown(*_) -> None:
    global _STOP
    _STOP = True
    log.warning("[%s] SIGTERM — draining: finishing current batch, no new claims", WORKER_ID)


def _execute_with_heartbeat(spec, run_id: str, samples_by_id: dict, ids: list[str]) -> dict:
    """Run the (possibly long) batch while a background daemon renews our lease — so an agentic/
    SWE-bench batch that outlives the claim lease isn't reclaimed by another worker mid-flight. The
    renewer uses its own thread-local PG connection; it stops the moment execution returns or raises
    (and if the whole worker dies, the lease simply lapses → reclaim, preserving crash safety)."""
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(LEASE_RENEW_SECONDS):
            try:
                db.control.renew_lease(run_id, ids, WORKER_ID)
                log.debug("[%s] run_id=%s renewed lease on %d task(s)", WORKER_ID, run_id, len(ids))
            except Exception:  # noqa: BLE001  a transient renew failure just risks one early reclaim
                log.debug("[%s] run_id=%s lease renew failed (will retry)", WORKER_ID, run_id)

    t = threading.Thread(target=beat, name=f"lease-{run_id}", daemon=True)
    t.start()
    try:
        return runner._execute_batch(spec, run_id, samples_by_id, ids)
    finally:
        stop.set()
        t.join(timeout=2)


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
        log.info("[%s] run_id=%s claimed %d sample(s)", WORKER_ID, run_id, len(ids))
        results = _execute_with_heartbeat(spec, run_id, samples_by_id, ids)
        # ack-before-flip commit: durable analytics insert → flip ledger 'done'; + retry + budget
        runner._commit_batch(spec, run_id, samples_by_id, ids, results)
        ok = sum(1 for r in results.values() if r and not r.get("error_type"))
        errs = sum(1 for r in results.values() if r and r.get("error_type"))
        missing = len(ids) - len(results)
        log.info("[%s] run_id=%s batch done: %d ok, %d errored, %d missing",
                 WORKER_ID, run_id, ok, errs, missing)
        processed += len(ids)
        # Refresh liveness between batches so a long multi-batch drain doesn't read as a dead worker.
        db.control.heartbeat("worker", WORKER_ID, {"claimed_this_loop": len(ids), "run": run_id})


def main() -> None:
    db.init()
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    log.info("[%s] up — poll=%.1fs, lease-renew=%.0fs", WORKER_ID, POLL_SECONDS, LEASE_RENEW_SECONDS)
    while True:
        if _STOP:
            log.info("[%s] drained — exiting", WORKER_ID)
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
