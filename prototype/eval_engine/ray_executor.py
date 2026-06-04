"""Distributed executor — Ray workers fan out over the SAME Postgres ledger (Phase 2).

The architectural leap: single-process ``runner.execute()`` → N Ray workers, each running the
*identical* claim → execute → commit → batch-load loop. The workers never coordinate with each
other — the Postgres ledger (``FOR UPDATE SKIP LOCKED``) is the sole coordinator, so a worker is
just a copy of the loop. That's the whole elegance: distribution = "run N copies of a thing
already proven exactly-once" (tests/test_concurrency_pg.py).

Requires the **postgres** backend: Ray workers are separate processes, and DuckDB can't take
concurrent multi-process writes (Postgres + ClickHouse can). KubeRay is *deployment*; a local
``ray.init()`` proves the *model*, which is what's risky.

The worker loop mirrors the concurrency-test worker (poll while anything is queued OR running)
rather than runner.execute()'s exit-on-first-empty: that's what gives **resumability** — if a
worker crashes holding a lease, survivors keep polling and reclaim its tasks once the lease
expires, instead of exiting and stranding them.
"""
from __future__ import annotations

import os
import time

import ray

from . import db


def _worker_loop(worker_id, run_id, spec, samples_by_id, batch_size, lease_seconds, crash_holding=False):
    from . import runner  # imported in the worker process: fresh registry + backend connections

    control = db.control
    processed = 0
    while True:
        ids = control.claim_batch(run_id, worker_id, batch_size, lease_seconds=lease_seconds)
        if not ids:
            c = control.counts(run_id)
            if c.get("queued", 0) == 0 and c.get("running", 0) == 0:
                return processed
            time.sleep(0.01)  # nothing claimable yet but peers still working / leases pending
            continue
        if crash_holding:
            os._exit(137)  # hard crash holding the lease, BEFORE commit — simulates worker death
        results = runner._execute_batch(spec, run_id, samples_by_id, ids)
        for sid in ids:
            if sid in results:
                control.commit_result(run_id, sid, results[sid])
            else:
                control.mark_failed(run_id, sid, "no_result")
        runner._batch_load(run_id, spec, ids)  # load only THIS worker's shard (no loader race)
        processed += len(ids)


@ray.remote
def ray_worker(worker_id, run_id, spec_dict, batch_size, lease_seconds, crash_holding=False):
    """A Ray task = one worker process. Rebuilds spec + dataset locally (Ray ships args, not state)."""
    from .datasets import load_jsonl
    from .models import RunSpec

    spec = RunSpec(**spec_dict)
    dataset, _ = load_jsonl(spec.dataset, spec.limit)
    samples_by_id = {str(s.id): s for s in dataset}
    return _worker_loop(worker_id, run_id, spec, samples_by_id, batch_size, lease_seconds, crash_holding)


def _ensure_ray() -> None:
    if not ray.is_initialized():
        # propagate the backend selection + DSNs so worker processes hit the same Postgres/ClickHouse
        env = {k: v for k, v in os.environ.items() if k.startswith("EVAL_ENGINE_")}
        ray.init(ignore_reinit_error=True, logging_level="ERROR", runtime_env={"env_vars": env})


def execute_distributed(run_id: str, spec, n_workers: int = 4, lease_seconds: float = 600.0) -> tuple[int, int, float]:
    """Coordinator: launch N Ray workers against the shared ledger, join, then finalize ONCE."""
    if db.BACKEND != "postgres":
        raise RuntimeError(
            "Ray distribution needs the postgres backend (Ray workers are separate processes and "
            "DuckDB can't take concurrent multi-process writes). Set EVAL_ENGINE_BACKEND=postgres."
        )
    from . import runner

    db.control.set_status(run_id, "running")
    _ensure_ray()
    spec_dict = spec.model_dump()
    futures = [
        ray_worker.remote(f"ray-{i}", run_id, spec_dict, spec.batch_size, lease_seconds)
        for i in range(n_workers)
    ]
    ray.get(futures)
    return runner._finalize(run_id, spec)


def run_distributed(spec, n_workers: int = 4, lease_seconds: float = 600.0) -> str:
    """Synchronous launch + distributed execute (the Ray analogue of runner.run)."""
    from . import runner

    run_id = runner.launch(spec)
    execute_distributed(run_id, spec, n_workers=n_workers, lease_seconds=lease_seconds)
    return run_id


__all__ = ["execute_distributed", "run_distributed", "ray_worker"]
