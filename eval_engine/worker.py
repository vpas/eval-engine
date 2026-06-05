"""Worker entrypoint — claim → execute → commit → load over the shared ledger.

Run N copies (a K8s Deployment, KEDA-scaled). The Postgres ledger (``FOR UPDATE SKIP LOCKED``) is
the sole coordinator, so a worker is just a copy of the loop — no inter-worker coordination. Crash
safety is the lease: a dead worker's claimed tasks are reclaimed by survivors once it expires.

    python -m eval_engine.worker          # EVAL_ENGINE_BACKEND=postgres in the cluster
"""
from __future__ import annotations

import os
import time

from . import db, runner
from .datasets import load_jsonl
from .models import RunSpec

POLL_SECONDS = float(os.environ.get("EVAL_ENGINE_WORKER_POLL", "1.0"))
WORKER_ID = os.environ.get("HOSTNAME", f"w-{os.getpid()}")  # pod name in K8s → unique claimer id


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
        ids = db.control.claim_batch(run_id, WORKER_ID, spec.batch_size)
        if not ids:
            return processed
        results = runner._execute_batch(spec, run_id, samples_by_id, ids)
        for sid in ids:
            runner._settle_result(run_id, sid, results.get(sid))  # commit, or retry-with-backoff to N
        runner._batch_load(run_id, spec, ids)  # load only our shard (no loader race)
        runner._enforce_budget(run_id, spec)   # stop claiming early once the run's budget is spent
        processed += len(ids)


def main() -> None:
    db.init()
    print(f"[worker {WORKER_ID}] up (backend={db.BACKEND})", flush=True)
    while True:
        did = 0
        for run_id in db.control.active_runs(("running",)):
            did += _drain_run(run_id)
        if did == 0:
            time.sleep(POLL_SECONDS)  # nothing claimable; let KEDA scale us down when idle


if __name__ == "__main__":
    main()
