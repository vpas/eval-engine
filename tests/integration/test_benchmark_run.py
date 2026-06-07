"""Integration: a benchmark subset runs through the full spine (ledger → analytics → rollup).

Uses the mock model (no provider key, no cost) on the committed MMLU subset — proving the real
benchmark data flows launch → expand → claim → execute → commit → load → finalize, and that
metadata.category lands as the ClickHouse group_key the per-category dashboard breakdown reads.
"""
from __future__ import annotations

from eval_engine import analytics, control, runner
from eval_engine.models import PluginRef, RunSpec

MMLU = "examples/benchmarks/mmlu.jsonl"


def test_mmlu_subset_runs_full_spine_with_category_rollup():
    spec = RunSpec(
        eval="mmlu_subjects", dataset=MMLU, model="mockllm/model", mock_output="ANSWER: D",
        harness=PluginRef(type="multiple_choice"), scorers=[PluginRef(type="choice")],
        batch_size=15,
    )
    run_id = runner.run(spec)  # synchronous launch + execute + finalize

    run = control.get_run(run_id)
    assert run["status"] == "completed", run  # finalized

    n, passed, _mean, _tokens, _cost = analytics.run_summary(run_id)
    assert n == 60, n  # every sample committed to analytics
    assert 0 <= passed <= n

    # The per-category breakdown the dashboard renders — MMLU spans multiple subjects.
    cats = {gk for gk, *_ in analytics.by_category(run_id)}
    assert len(cats) >= 3, cats
    assert "high_school_mathematics" in cats

    # ledger pruned at finalize (ephemeral), per ORCHESTRATION §10
    assert control.ledger_size(run_id) == 0


def test_gsm8k_numeric_scorer_runs_full_spine():
    # A correct fixed answer ('ANSWER: 17') passes exactly the samples whose target is 17 — proving
    # the numeric_answer scorer drives pass/fail end-to-end (not just that the run completes).
    spec = RunSpec(
        eval="gsm8k", dataset="examples/benchmarks/gsm8k.jsonl", model="mockllm/model",
        mock_output="Reasoning... ANSWER: 17",
        harness=PluginRef(type="single_turn"), scorers=[PluginRef(type="numeric_answer")],
        batch_size=10,
    )
    run_id = runner.run(spec)
    n, passed, *_ = analytics.run_summary(run_id)
    assert n == 50, n
    assert passed >= 1, "expected the samples whose answer is 17 to pass"
