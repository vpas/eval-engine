"""Validate the Phase 2 leap: Ray workers fan out over the SAME Postgres ledger.

Single-process execute() is proven; this proves N separate worker PROCESSES claiming the shared
ledger via FOR UPDATE SKIP LOCKED give the same guarantees, plus crash resumability — with NO
coordinator (the ledger is the coordinator).

  1. EXACTLY-ONCE (distributed): N Ray workers run a real end-to-end run (claim → Inspect →
     commit → batch-load to ClickHouse). Every sample lands in ClickHouse exactly once, the
     ledger prunes to 0, done == M.
  2. CRASH RECLAIM: one worker hard-crashes (os._exit) holding a claimed, uncommitted batch.
     Survivors keep polling, reclaim its tasks once the lease expires, and the run still
     completes — resumability without a coordinator.

Requires postgres backend + docker PG/CH (infra/up.sh) + ray:
  EVAL_ENGINE_BACKEND=postgres PYTHONPATH=. ../.venv/bin/python tests/test_distributed_ray.py
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import ray

from eval_engine import runner
from eval_engine.db import BACKEND, analytics, control
from eval_engine.models import PluginRef, RunSpec
from eval_engine.ray_executor import _ensure_ray, execute_distributed, ray_worker


def _dataset(m: int) -> str:
    cats = ["geography", "math", "science"]
    tmp = Path(tempfile.mkdtemp()) / "big.jsonl"
    with tmp.open("w") as f:
        for i in range(m):
            f.write(json.dumps({
                "id": f"s{i:05d}",
                "input": "What is the capital of France? Answer with one word.",
                "target": "Paris",  # mock always says "Paris" → every sample passes (deterministic)
                "metadata": {"category": cats[i % len(cats)]},
            }) + "\n")
    return str(tmp)


def _spec(m: int, batch: int) -> RunSpec:
    return RunSpec(
        eval="dist_stress", dataset=_dataset(m), model="mockllm/model", mock_output="Paris",
        batch_size=batch, harness=PluginRef(type="single_turn"),
        scorers=[PluginRef(type="includes", config={"ignore_case": True})],
    )


def _cleanup(run_id: str) -> None:
    con = control._conn()
    con.execute("DELETE FROM sample_tasks WHERE run_id=%s", (run_id,))
    con.execute("DELETE FROM failed_task_archive WHERE run_id=%s", (run_id,))
    con.execute("DELETE FROM runs WHERE id=%s", (run_id,))


def _attempts(run_id: str) -> int:
    return control._conn().execute(
        "SELECT coalesce(sum(attempts),0) FROM sample_tasks WHERE run_id=%s", (run_id,)
    ).fetchone()[0]


def test_exactly_once_distributed(M=300, N=4, batch=25):
    spec = _spec(M, batch)
    run_id = runner.launch(spec)
    try:
        t0 = time.time()
        done, failed, acc = execute_distributed(run_id, spec, n_workers=N)
        dt = time.time() - t0
        # ClickHouse is the source of truth — FINAL collapses any ReplacingMergeTree dupes, so a
        # row count != M would mean a real double-load or a dropped sample.
        n_rows, passed, *_ = analytics.run_summary(run_id)
        assert done == M, f"done {done} != {M}"
        assert n_rows == M, f"ClickHouse has {n_rows} rows != {M} (double-load or drop!)"
        assert passed == M, f"passed {passed} != {M}"
        assert control.ledger_size(run_id) == 0, "ledger not pruned after finalize"
        print(f"  [exactly-once/dist] {M} samples / {N} ray workers in {dt:.1f}s → "
              f"ClickHouse rows={n_rows} done={done} acc={acc:.2f}  pruned ✓  no double-load ✓")
    finally:
        _cleanup(run_id)


def test_crash_reclaim(M=400, n_good=4, batch=25, lease=2.0):
    spec = _spec(M, batch)
    run_id = runner.launch(spec)
    try:
        control.set_status(run_id, "running")
        _ensure_ray()
        sd = spec.model_dump()
        # doomed claims one batch then os._exit BEFORE committing — a worker death holding a lease.
        # max_retries=0 makes the crash terminal (no Ray auto-retry muddying the reclaim count).
        doomed = ray_worker.options(max_retries=0).remote("doomed", run_id, sd, batch, lease, True)
        good = [ray_worker.remote(f"g{i}", run_id, sd, batch, lease) for i in range(n_good)]

        ray.get(good)                       # returns only when queued==0 AND running==0 (all done)
        try:
            ray.get(doomed)                 # the crashed worker's future raises — expected
        except Exception:
            pass

        # inspect the ledger BEFORE _finalize prunes it
        attempts, cnt = _attempts(run_id), control.counts(run_id)
        assert cnt.get("done", 0) == M, f"done {cnt.get('done', 0)} != {M} (crash stranded tasks!)"
        assert attempts > M, f"attempts {attempts} == {M}: no reclaim (crash not exercised — retune)"
        reclaimed = attempts - M

        runner._finalize(run_id, spec)
        n_rows, *_ = analytics.run_summary(run_id)
        assert n_rows == M, f"ClickHouse rows {n_rows} != {M}"
        print(f"  [crash-reclaim] doomed worker died holding a batch; survivors reclaimed "
              f"{reclaimed} task(s) after the {lease}s lease → done={M} ClickHouse rows={n_rows} ✓")
    finally:
        _cleanup(run_id)


if __name__ == "__main__":
    if BACKEND != "postgres":
        raise SystemExit("set EVAL_ENGINE_BACKEND=postgres (Ray distribution needs the real backends)")
    print("Ray distributed executor over the shared Postgres ledger:")
    test_exactly_once_distributed()
    test_crash_reclaim()
    ray.shutdown()
    print("\nALL PASS ✓")
