"""Validate the REAL claim — Postgres ``FOR UPDATE SKIP LOCKED`` — under concurrency.

Unlike SQLite (which serializes all writes behind one lock), Postgres lets N workers claim
*different* rows truly in parallel via row-level locks + SKIP LOCKED. This is the actual
ORCHESTRATION §6 primitive the distributed system depends on. Proves exactly-once + reclaim.

Requires the docker Postgres (EVAL_ENGINE_PG_DSN). Per-run scoped, so it doesn't disturb other data.
Run:  PYTHONPATH=. ../.venv/bin/python tests/test_concurrency_pg.py
"""
from __future__ import annotations

import threading
import time

from eval_engine import control_pg as control


def _fake_result() -> dict:
    return {
        "passed": 1, "primary_score": 1.0, "scores": {"includes": 1.0},
        "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0, "latency_ms": 0,
        "error_type": "", "transcript_uri": "",
    }


def _make_run(n: int) -> str:
    control.init()
    run_id = control.new_run_id()
    control.create_run({
        "id": run_id, "eval_id": "stress_pg", "eval_version": 1, "model": "x/y",
        "provider": "x", "model_id": "y", "harness": "single_turn", "scorers": [],
        "total": n, "dataset_hash": "-",
    })
    control.expand_tasks(run_id, [(f"s{i:05d}", "cat") for i in range(n)])
    return run_id


def _sum_attempts(run_id: str) -> int:
    return control._conn().execute(
        "SELECT coalesce(sum(attempts),0) FROM sample_tasks WHERE run_id=%s", (run_id,)
    ).fetchone()[0]


def _cleanup(run_id: str) -> None:
    """Remove everything this test wrote — it runs against the shared dev DB, so leave no residue."""
    con = control._conn()
    con.execute("DELETE FROM sample_tasks WHERE run_id=%s", (run_id,))
    con.execute("DELETE FROM failed_task_archive WHERE run_id=%s", (run_id,))
    con.execute("DELETE FROM runs WHERE id=%s", (run_id,))


def test_exactly_once(M=2000, N=12, batch=13):
    run_id = _make_run(M)
    try:
        processed: list[tuple[str, str]] = []
        lock = threading.Lock()

        def worker(w: int):
            wid = f"w{w}"
            while True:
                ids = control.claim_batch(run_id, wid, batch)
                if not ids:
                    c = control.counts(run_id)
                    if c.get("queued", 0) == 0 and c.get("running", 0) == 0:
                        return
                    time.sleep(0.003)
                    continue
                for sid in ids:
                    control.commit_result(run_id, sid, _fake_result())
                with lock:
                    processed.extend((wid, sid) for sid in ids)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        dt = time.time() - t0

        ids = [sid for _, sid in processed]
        assert len(ids) == M, f"processed {len(ids)} != {M}"
        assert len(set(ids)) == M, f"DOUBLE-CLAIM: {len(ids) - len(set(ids))} duplicate(s)"
        assert control.counts(run_id).get("done", 0) == M
        assert _sum_attempts(run_id) == M, f"attempts={_sum_attempts(run_id)} != {M}"

        per_worker = {}
        for w, _ in processed:
            per_worker[w] = per_worker.get(w, 0) + 1
        print(f"  [exactly-once] {M} samples / {N} workers / batch {batch} in {dt*1000:.0f}ms "
              f"({M/dt:.0f} samples/s)")
        print(f"     exactly-once ✓  no double-claim ✓  attempts==samples ✓")
        print(f"     all {N} workers active: {len(per_worker) == N}  split={dict(sorted(per_worker.items()))}")
    finally:
        _cleanup(run_id)


def test_lease_reclaim():
    run_id = _make_run(10)
    try:
        a = control.claim_batch(run_id, "A", 5, lease_seconds=1.0)  # A grabs 5, then "crashes"
        assert len(a) == 5

        b = control.claim_batch(run_id, "B", 10, lease_seconds=1.0)  # B gets the other 5
        assert len(b) == 5 and set(b).isdisjoint(a), "B stole A's live tasks!"
        for sid in b:
            control.commit_result(run_id, sid, _fake_result())

        c_early = control.claim_batch(run_id, "C", 10, lease_seconds=60)  # A's lease still valid
        assert c_early == [], f"reclaimed live tasks early: {c_early}"

        time.sleep(1.3)
        c = control.claim_batch(run_id, "C", 10, lease_seconds=60)  # reclaim A's expired tasks
        assert set(c) == set(a), f"reclaim mismatch: {set(c)} vs {set(a)}"
        print("  [lease-reclaim] live tasks not stolen ✓  early-reclaim blocked ✓  expired reclaimed ✓")
    finally:
        _cleanup(run_id)


def test_retry_backoff():
    """Transient failure re-queues with a not_before backoff (unclaimable until it elapses), to the
    attempt cap, then terminal 'failed' — on the REAL Postgres backend (FR5, ORCHESTRATION §7)."""
    run_id = _make_run(1)
    sid = "s00000"
    try:
        assert control.claim_batch(run_id, "W", 1) == [sid]               # attempt 1
        assert control.retry_or_fail(run_id, sid, "boom", max_attempts=2, base_seconds=0.5) == "retry"
        assert control.counts(run_id).get("queued", 0) == 1
        assert control.claim_batch(run_id, "W", 1) == [], "claimed during backoff"
        time.sleep(0.7)
        assert control.claim_batch(run_id, "W", 1) == [sid], "not reclaimed after backoff"  # attempt 2
        assert control.retry_or_fail(run_id, sid, "boom", max_attempts=2, base_seconds=0.5) == "failed"
        assert control.counts(run_id).get("failed", 0) == 1
        time.sleep(0.7)
        assert control.claim_batch(run_id, "W", 1) == [], "terminal-failed task re-claimed"
        print("  [retry-backoff] re-queue ✓  not_before blocks claim ✓  attempt-cap → terminal ✓")
    finally:
        _cleanup(run_id)


def test_budget_stop():
    """Budget cap → remaining queued become the DISTINCT terminal 'budget_skipped' (not 'failed'),
    claim-terminal, archived — on the REAL Postgres backend (DESIGN §8, FR6)."""
    run_id = _make_run(5)
    try:
        ids = control.claim_batch(run_id, "W", 2)
        for sid in ids:
            control.commit_result(run_id, sid, {**_fake_result(), "cost_usd": 0.40})
        assert abs(control.run_cost(run_id) - 0.80) < 1e-9, control.run_cost(run_id)
        assert control.budget_stop(run_id) == 3
        c = control.counts(run_id)
        assert c.get("done") == 2 and c.get("budget_skipped") == 3 and c.get("queued", 0) == 0, c
        assert "failed" not in c, "budget skip must NOT inflate failed_samples"
        assert control.claim_batch(run_id, "W", 9) == [], "budget_skipped task re-claimed"
        control.archive_and_prune(run_id)
        arch = dict(control._conn().execute(
            "SELECT error_type, count(*) FROM failed_task_archive WHERE run_id=%s GROUP BY error_type",
            (run_id,)).fetchall())
        assert arch == {"budget_exceeded": 3}, arch
        print("  [budget] cost gauge ✓  queued→budget_skipped (not failed) ✓  claim-terminal ✓  archived ✓")
    finally:
        _cleanup(run_id)


def test_live_rollup():
    """Orchestrator live rollup → runs row, on the REAL Postgres backend (DESIGN §8)."""
    run_id = _make_run(4)
    try:
        ids = control.claim_batch(run_id, "W", 2)
        control.commit_result(run_id, ids[0], {**_fake_result(), "passed": 1, "cost_usd": 0.10})
        control.commit_result(run_id, ids[1], {**_fake_result(), "passed": 0, "cost_usd": 0.20})
        done, failed, passed, cost = control.live_rollup(run_id)
        assert (done, failed, passed) == (2, 0, 1) and abs(cost - 0.30) < 1e-9, (done, failed, passed, cost)
        control.update_live(run_id, done, failed, passed / done, cost)
        run = control.get_run(run_id)  # RUN_COLS: …done(10) failed(11) accuracy(12) cost_usd(13)
        assert run[8] == "queued" and run[10] == 2 and run[12] == 0.5 and abs(run[13] - 0.30) < 1e-9, run
        print("  [live-rollup] done/passed/cost gauge ✓  written to runs row ✓  get_run cols aligned ✓")
    finally:
        _cleanup(run_id)


if __name__ == "__main__":
    print("Postgres FOR UPDATE SKIP LOCKED concurrency tests:")
    test_exactly_once()
    test_lease_reclaim()
    test_retry_backoff()
    test_budget_stop()
    test_live_rollup()
    print("\nALL PASS ✓")
