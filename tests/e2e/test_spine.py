"""Worker + Orchestrator split over the shared ledger — the full distributed spine in-process.

Exercises the role entrypoints: launch (persist RunSpec) → orchestrator admit (queued→running) →
worker drain (claim→execute→commit→load) → orchestrator finalize. Same exactly-once result path as
the single-process runner, now via the api/worker/orchestrator roles. Schema + per-test isolation
come from the autouse ``clean_db`` fixture (tests/e2e/conftest.py); the spec from ``mock_spec``.
"""
from eval_engine import db, orchestrator, runner, worker
from eval_engine.datasets import load_jsonl
from eval_engine.models import RunSpec


def test_worker_orchestrator_split(mock_spec):
    spec = mock_spec()  # single_turn mock, 'Paris' → 1/3 on examples/qa.jsonl

    # 1) launch persists the full RunSpec so a separate process can rehydrate it
    run_id = runner.launch(spec)
    spec_json = db.control.get_spec(run_id)
    assert spec_json, "RunSpec not persisted"
    assert RunSpec.model_validate_json(spec_json).dataset == "examples/qa.jsonl"
    assert run_id in db.control.active_runs(("queued",))
    assert db.control.run_total(run_id) == 3

    # reproducibility pins recorded on the run (DESIGN §14). RUN_COLS: eval_version(2), image_digest(17)
    rr = db.control.get_run(run_id)
    assert rr["eval_version"] == 1 and rr["image_digest"] is not None, \
        f"repro pins not recorded: eval_version={rr['eval_version']} image={rr['image_digest']}"

    # 2) orchestrator admits queued → running
    orchestrator.tick()
    assert run_id in db.control.active_runs(("running",))

    # 3) worker drains all tasks (claim→execute→commit→load)
    processed = worker._drain_run(run_id)
    assert processed == 3, f"expected 3 processed, got {processed}"

    # ack-before-flip invariant (DESIGN §8): every 'done' row is ALREADY durable in analytics — the
    # insert precedes the flip, so there are no done-but-unloaded rows (done ⟹ durable).
    assert db.control.fetch_unloaded(run_id) == [], "done rows not loaded — ack-before-flip violated"
    assert db.analytics.run_summary(run_id)[0] == 3, "analytics missing rows before finalize"

    # 4) orchestrator finalizes: aggregate → archive → prune → completed
    orchestrator.tick()
    run = db.control.get_run(run_id)
    assert run["status"] == "completed", f"status={run['status']}"
    assert db.control.ledger_size(run_id) == 0, "ledger not pruned"

    # exactly-once result landed in analytics; mock 'Paris' → 1/3 correct
    n, passed, *_ = db.analytics.run_summary(run_id)
    assert n == 3 and passed == 1, f"n={n} passed={passed}"


def test_epochs_reduce_to_one_row_per_sample(mock_spec):
    """Epochs repeat each sample N× and Inspect reduces them to one per-sample row (DESIGN §14, FR8),
    and the Wilson CI brackets the observed pass rate."""
    run_id = runner.run(mock_spec(eval="epochs_qa", epochs=3, batch_size=3))
    n, passed, *_ = db.analytics.run_summary(run_id)
    assert n == 3, f"epochs should reduce 3×-repeated samples to 3 rows, got n={n}"  # not 9

    lo, hi = runner.wilson_ci(int(passed), int(n))
    assert 0.0 <= lo <= passed / n <= hi <= 1.0, (lo, passed / n, hi)  # CI brackets the rate


def test_provider_fingerprint_pinned(mock_spec):
    """A run records the provider's resolved-model version fingerprint (DESIGN §14, backlog #7).
    The mock echoes its model name back as ModelOutput.model → pinned on the run."""
    run_id = runner.run(mock_spec())
    fp = db.control.get_run(run_id)["provider_fingerprint"]
    assert fp == "mockllm/model", fp


def _drain_one_batch(spec, run_id: str) -> int:
    """Claim + execute + commit a SINGLE batch of a running run (one iteration of worker._drain_run),
    so a test can advance a run partway and then act on it mid-flight."""
    dataset, _ = load_jsonl(spec.dataset, spec.limit)
    samples_by_id = {str(s.id): s for s in dataset}
    ids = db.control.claim_batch(run_id, "w-test", spec.batch_size)
    if not ids:
        return 0
    results = runner.execute_batch(spec, run_id, samples_by_id, ids)
    runner.commit_batch(spec, run_id, samples_by_id, ids, results)
    return len(ids)


def test_cancel_mid_run_keeps_finished_samples_and_settles_cancelled(mock_spec):
    """A different terminal lifecycle than the happy path: cancel AFTER some samples have committed.
    The already-done sample's result is retained in analytics, the still-queued ones are cancelled, the
    ledger is pruned, and the run settles as ``cancelled`` (DESIGN §8 cancel = stop-scheduling +
    finalize-as-cancelled). Drives the spine launch → admit → partial drain → cancel → terminal."""
    spec = mock_spec(batch_size=1)  # 3 samples, one per batch → easy to stop after the first
    run_id = runner.launch(spec)
    orchestrator.tick()                              # queued → running
    assert run_id in db.control.active_runs(("running",))

    assert _drain_one_batch(spec, run_id) == 1       # one sample committed; two still queued
    assert db.analytics.run_summary(run_id)[0] == 1, "the finished sample must be durable before cancel"

    summary = db.control.cancel_run(run_id)
    assert summary == {"cancelled_queued": 2, "done": 1, "failed": 0}, summary

    run = db.control.get_run(run_id)
    assert run["status"] == "cancelled" and run["done"] == 1
    assert db.control.ledger_size(run_id) == 0, "ledger should be pruned at cancel, like any finalize"
    # the partial result survives the cancel — a cancelled run still shows what it managed to score.
    assert db.analytics.run_summary(run_id)[0] == 1

    # idempotent: cancelling an already-terminal run is a no-op (no double-finalize).
    assert db.control.cancel_run(run_id) is None
