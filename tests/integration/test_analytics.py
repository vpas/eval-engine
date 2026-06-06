"""Analytics projection (ClickHouse) in isolation — insert + the slice queries the API reads.

Covers the one non-obvious invariant: ReplacingMergeTree(attempt) keyed on
(eval_id, model_id, run_id, sample_id) — a re-executed sample's HIGHER attempt wins and duplicate
re-inserts collapse, read exactly via ``FINAL`` (ORCHESTRATION §11, the retry-wins guarantee the
ack-before-flip commit relies on). Analytics is run_id-scoped (not truncated between tests), so each
test uses a fresh run_id and asserts only its own rows.
"""
from __future__ import annotations

import datetime

from eval_engine import analytics, control

_FIN = datetime.datetime(2026, 6, 1, 12, 0, 0)


def _row(run_id: str, sid: str, **over) -> tuple:
    """One sample_results row in analytics._COLUMNS order (override any field via kwargs)."""
    f = dict(eval_id="an_eval", eval_version=1, provider="prov", model_id="m1",
             harness_type="single_turn", group_key="cat", passed=1, primary_score=1.0,
             scores='{"includes":1.0}', tokens_in=10, tokens_out=20, cost_usd=0.5,
             latency_ms=0, attempt=1, error_type="", transcript_uri="", review_status="none",
             finished_at=_FIN)
    f.update(over)
    return (run_id, sid, f["eval_id"], f["eval_version"], f["provider"], f["model_id"],
            f["harness_type"], f["group_key"], f["passed"], f["primary_score"], f["scores"],
            f["tokens_in"], f["tokens_out"], f["cost_usd"], f["latency_ms"], f["attempt"],
            f["error_type"], f["transcript_uri"], f["review_status"], f["finished_at"])


def test_insert_and_run_summary():
    run_id = control.new_run_id()
    analytics.insert([
        _row(run_id, "s0", passed=1, primary_score=1.0, tokens_in=10, tokens_out=20, cost_usd=0.5),
        _row(run_id, "s1", passed=1, primary_score=1.0, tokens_in=10, tokens_out=20, cost_usd=0.5),
        _row(run_id, "s2", passed=0, primary_score=0.0, tokens_in=10, tokens_out=20, cost_usd=0.5),
    ])
    n, passed, mean, tokens, cost = analytics.run_summary(run_id)
    assert n == 3 and passed == 2, (n, passed)
    assert abs(mean - (2 / 3)) < 1e-9, mean              # avg primary_score
    assert tokens == 90 and abs(cost - 1.5) < 1e-9, (tokens, cost)


def test_empty_insert_is_noop():
    analytics.insert([])  # must not raise; nothing to assert beyond that


def test_run_summary_zero_rows_is_not_nan():
    # A run with no committed rows (all samples failed) must summarize to zeros — ClickHouse avg()
    # over an empty set returns NaN, which would otherwise break JSON serialization at the API.
    n, passed, mean, tokens, cost = analytics.run_summary(control.new_run_id())
    assert (n, passed, mean, tokens, cost) == (0, 0, 0, 0, 0)


def test_replacing_merge_tree_higher_attempt_wins():
    """A retried sample re-inserts with a higher ``attempt`` (the RMT version); ``FINAL`` collapses
    the duplicate to the latest attempt — so a successful retry overwrites the earlier failure."""
    run_id = control.new_run_id()
    analytics.insert([_row(run_id, "s0", attempt=1, passed=0, primary_score=0.0)])  # first try: fail
    analytics.insert([_row(run_id, "s0", attempt=2, passed=1, primary_score=1.0)])  # retry: pass
    n, passed, *_ = analytics.run_summary(run_id)
    assert n == 1, f"duplicate not collapsed by FINAL: n={n}"
    assert passed == 1, "higher attempt (the retry) did not win"
    # the per-sample slice reflects the winning attempt too
    sid, p, _gk, score, _uri = analytics.samples(run_id)[0]
    assert (sid, p, score) == ("s0", 1, 1.0), (sid, p, score)


def test_by_category_groups_and_orders():
    run_id = control.new_run_id()
    analytics.insert([
        _row(run_id, "a0", group_key="alpha", passed=1),
        _row(run_id, "a1", group_key="alpha", passed=0),
        _row(run_id, "b0", group_key="beta", passed=1),
    ])
    rows = analytics.by_category(run_id)
    by = {gk: (n, passed) for gk, n, passed, _acc in rows}
    assert by == {"alpha": (2, 1), "beta": (1, 1)}, by
    assert [r[0] for r in rows] == ["alpha", "beta"], "by_category not ordered by group_key"


def test_samples_ordered_by_id():
    run_id = control.new_run_id()
    analytics.insert([
        _row(run_id, "s2", transcript_uri="u2"),
        _row(run_id, "s0", transcript_uri="u0"),
        _row(run_id, "s1", transcript_uri="u1"),
    ])
    ids = [sid for sid, *_ in analytics.samples(run_id)]
    assert ids == ["s0", "s1", "s2"], ids
