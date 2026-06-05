"""Worker + Orchestrator split over the shared ledger (replaces the old Ray test).

Exercises the M3 entrypoints in-process: launch (persist RunSpec) → orchestrator admit
(queued→running) → worker drain (claim→execute→commit→load) → orchestrator finalize. Same
exactly-once result path as the single-process runner, now via the api/worker/orchestrator roles.
"""
from eval_engine import db, orchestrator, runner, worker
from eval_engine.models import PluginRef, RunSpec


def test_worker_orchestrator_split():
    spec = RunSpec(
        eval="capitals_qa",
        dataset="examples/qa.jsonl",
        model="mockllm/model",
        mock_output="Paris",
        batch_size=2,
        harness=PluginRef(type="single_turn"),
        scorers=[PluginRef(type="includes", config={"ignore_case": True})],
    )
    db.init()

    # 1) launch persists the full RunSpec so a separate process can rehydrate it
    run_id = runner.launch(spec)
    spec_json = db.control.get_spec(run_id)
    assert spec_json, "RunSpec not persisted"
    assert RunSpec.model_validate_json(spec_json).dataset == "examples/qa.jsonl"
    assert run_id in db.control.active_runs(("queued",))
    assert db.control.run_total(run_id) == 3

    # reproducibility pins recorded on the run (DESIGN §14). RUN_COLS: eval_version(2), image_digest(17)
    rr = db.control.get_run(run_id)
    assert rr[2] == 1 and rr[17] is not None, f"repro pins not recorded: eval_version={rr[2]} image={rr[17]}"

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
    status = run[8]  # id,eval_id,eval_version,model,provider,model_id,harness,scorers,status,...
    assert status == "completed", f"status={status}"
    assert db.control.ledger_size(run_id) == 0, "ledger not pruned"

    # exactly-once result landed in analytics; mock 'Paris' → 1/3 correct
    n, passed, *_ = db.analytics.run_summary(run_id)
    assert n == 3 and passed == 1, f"n={n} passed={passed}"
    print(f"worker/orchestrator split ✓  (run {run_id}: {passed}/{n} passed, ledger pruned)")


def test_epochs_and_ci():
    """Epochs repeat each sample (Inspect reduces to one per-sample row) and the Wilson CI brackets
    the pass rate (DESIGN §14, FR8)."""
    lo, hi = runner.wilson_ci(50, 100)
    assert abs(lo - 0.404) < 0.01 and abs(hi - 0.596) < 0.01, (lo, hi)
    assert runner.wilson_ci(0, 0) == (0.0, 0.0)
    lo, hi = runner.wilson_ci(10, 10)
    assert hi <= 1.0 and lo > 0.7, (lo, hi)  # never escapes [0,1]

    spec = RunSpec(
        eval="epochs_qa", dataset="examples/qa.jsonl", model="mockllm/model", mock_output="Paris",
        epochs=3, batch_size=3,
        harness=PluginRef(type="single_turn"),
        scorers=[PluginRef(type="includes", config={"ignore_case": True})],
    )
    db.init()
    run_id = runner.run(spec)
    n, passed, *_ = db.analytics.run_summary(run_id)
    assert n == 3, f"epochs should reduce to one row per sample, got n={n}"  # 3 samples, not 9
    ci = runner.wilson_ci(50, 100)
    print(f"epochs+CI ✓  (epochs=3 → {n} reduced rows; wilson_ci(50,100)=[{ci[0]:.3f},{ci[1]:.3f}])")


if __name__ == "__main__":
    test_worker_orchestrator_split()
    test_epochs_and_ci()
    print("ALL PASS ✓")
