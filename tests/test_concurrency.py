"""Stress the ledger claim/commit under real concurrency.

Proves the two correctness properties that matter most (and are easiest to get wrong):
  1. EXACTLY-ONCE: with N threads claiming a shared ledger, every sample is processed by
     exactly one worker — no double-claim, none dropped.
  2. LEASE RECLAIM: a task abandoned by a "crashed" worker is reclaimed after its lease
     expires, and live (un-expired) tasks are NOT stolen.

Isolated from Inspect (work is simulated) and from the demo DB (uses a temp SQLite file).
Run:  PYTHONPATH=. ../.venv/bin/python tests/test_concurrency.py
"""
from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from eval_engine import control


def _use_temp_db():
    tmp = Path(tempfile.mkdtemp())
    control.DATA = tmp
    control.DB = str(tmp / "control_test.db")


def _fake_result() -> dict:
    return {
        "passed": 1, "primary_score": 1.0, "scores": {"includes": 1.0},
        "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0, "latency_ms": 0,
        "error_type": "", "transcript_uri": "",
    }


def _make_run(n_samples: int) -> str:
    run_id = control.new_run_id()
    control.create_run({
        "id": run_id, "eval_id": "stress", "eval_version": 1, "model": "x/y",
        "provider": "x", "model_id": "y", "harness": "single_turn", "scorers": [],
        "total": n_samples, "dataset_hash": "-",
    })
    control.expand_tasks(run_id, [(f"s{i:04d}", "cat") for i in range(n_samples)])
    return run_id


def test_exactly_once(M=400, N=8, batch=9):
    run_id = _make_run(M)
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
    counts = control.counts(run_id)
    total_attempts = _sum_attempts(run_id)

    assert len(ids) == M, f"processed {len(ids)} != {M}"
    assert len(set(ids)) == M, f"DOUBLE-CLAIM: {len(ids) - len(set(ids))} duplicate(s)"
    assert counts.get("done", 0) == M, f"done={counts.get('done')} != {M}"
    assert total_attempts == M, f"attempts={total_attempts} != {M} (unexpected retries/reclaims)"

    per_worker = {}
    for w, _ in processed:
        per_worker[w] = per_worker.get(w, 0) + 1
    print(f"  [exactly-once] {M} samples / {N} workers / batch {batch} in {dt*1000:.0f}ms")
    print(f"     exactly-once ✓  no double-claim ✓  attempts==samples ✓")
    print(f"     work split: {dict(sorted(per_worker.items()))}")


def test_lease_reclaim():
    run_id = _make_run(10)

    a = control.claim_batch(run_id, "A", 5, lease_seconds=1.0)  # worker A grabs 5, then "crashes"
    assert len(a) == 5

    b = control.claim_batch(run_id, "B", 10, lease_seconds=1.0)  # B must get the OTHER 5
    assert len(b) == 5, f"B claimed {len(b)}"
    assert set(b).isdisjoint(a), "B stole A's live (un-expired) tasks!"
    for sid in b:
        control.commit_result(run_id, sid, _fake_result())

    # Before A's lease expires, C must NOT be able to reclaim A's tasks.
    c_early = control.claim_batch(run_id, "C", 10, lease_seconds=60)
    assert c_early == [], f"reclaimed live tasks early: {c_early}"

    time.sleep(1.2)  # let A's lease expire
    c = control.claim_batch(run_id, "C", 10, lease_seconds=60)  # C reclaims A's abandoned 5
    assert set(c) == set(a), f"reclaim mismatch: {set(c)} vs {set(a)}"

    print(f"  [lease-reclaim] live tasks not stolen ✓  early-reclaim blocked ✓  "
          f"expired tasks reclaimed ✓ (attempts now 2 on those)")


def _sum_attempts(run_id: str) -> int:
    con = control._con()
    n = con.execute("SELECT coalesce(sum(attempts),0) FROM sample_tasks WHERE run_id=?", (run_id,)).fetchone()[0]
    con.close()
    return n


def _status(run_id: str, sid: str) -> str:
    con = control._con()
    s = con.execute("SELECT status FROM sample_tasks WHERE run_id=? AND sample_id=?", (run_id, sid)).fetchone()[0]
    con.close()
    return s


def test_retry_backoff():
    """A transient failure re-queues with a not_before backoff (not claimable until it elapses),
    up to the attempt cap, then goes terminal 'failed' (FR5, ORCHESTRATION §7)."""
    run_id = _make_run(1)
    sid = "s0000"

    # attempt 1: claim → fail. max_attempts=2 so this re-queues (not terminal yet).
    assert control.claim_batch(run_id, "W", 1) == [sid]
    out = control.retry_or_fail(run_id, sid, "boom", max_attempts=2, base_seconds=0.5, cap_seconds=10)
    assert out == "retry", out
    assert _status(run_id, sid) == "queued"

    # backoff active: NOT claimable until not_before elapses (poison sample doesn't head-of-line block).
    assert control.claim_batch(run_id, "W", 1) == [], "claimed during backoff"
    time.sleep(0.6)
    assert control.claim_batch(run_id, "W", 1) == [sid], "not reclaimed after backoff"  # attempt 2

    # attempt 2 fails → at the cap → terminal 'failed', and it stays out of the claimable set.
    out = control.retry_or_fail(run_id, sid, "boom", max_attempts=2, base_seconds=0.5, cap_seconds=10)
    assert out == "failed", out
    assert _status(run_id, sid) == "failed"
    time.sleep(0.6)
    assert control.claim_batch(run_id, "W", 1) == [], "terminal-failed task re-claimed"
    print("  [retry-backoff] re-queue ✓  not_before blocks claim ✓  attempt-cap → terminal ✓")


def test_budget_stop():
    """When committed cost reaches the budget, remaining queued samples become the DISTINCT terminal
    `budget_skipped` (not `failed`), are never re-claimed, and are archived (DESIGN §8, FR6)."""
    run_id = _make_run(5)
    ids = control.claim_batch(run_id, "W", 2)            # run 2 of 5 at $0.40 each → cost $0.80
    for sid in ids:
        control.commit_result(run_id, sid, {**_fake_result(), "cost_usd": 0.40})
    assert abs(control.run_cost(run_id) - 0.80) < 1e-9, control.run_cost(run_id)

    skipped = control.budget_stop(run_id)               # budget already blown → skip the queued 3
    assert skipped == 3, skipped
    c = control.counts(run_id)
    assert c.get("done") == 2 and c.get("budget_skipped") == 3 and c.get("queued", 0) == 0, c
    assert "failed" not in c, "budget skip must NOT inflate failed_samples"
    assert control.claim_batch(run_id, "W", 9) == [], "budget_skipped task re-claimed"

    control.archive_and_prune(run_id)                   # recorded on prune
    con = control._con()
    arch = dict(con.execute(
        "SELECT error_type, count(*) FROM failed_task_archive WHERE run_id=? GROUP BY error_type",
        (run_id,)).fetchall())
    con.close()
    assert arch == {"budget_exceeded": 3}, arch
    print("  [budget] cost gauge ✓  queued→budget_skipped (not failed) ✓  claim-terminal ✓  archived ✓")


if __name__ == "__main__":
    _use_temp_db()
    print("concurrency tests (ledger claim/commit):")
    test_exactly_once()
    test_lease_reclaim()
    test_retry_backoff()
    test_budget_stop()
    print("\nALL PASS ✓")
