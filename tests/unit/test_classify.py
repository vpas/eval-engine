"""Unit: run lane classification (SCHEDULER §2) — pure function, no backends."""
from eval_engine.models import PluginRef, RunSpec
from eval_engine.runner import _classify


def _spec(**over) -> RunSpec:
    return RunSpec(eval="e", dataset="d", harness=PluginRef(type="single_turn"), scorers=[], **over)


def test_limit_or_small_total_is_interactive():
    assert _classify(_spec(limit=10), 5000) == ("interactive", 5)  # a subset/limit ⇒ interactive
    assert _classify(_spec(), 50) == ("interactive", 5)            # small total ⇒ interactive


def test_large_total_is_batch():
    assert _classify(_spec(), 5000) == ("batch", 50)


def test_explicit_lane_overrides_size():
    assert _classify(_spec(lane="batch"), 10) == ("batch", 50)
    assert _classify(_spec(lane="interactive"), 99999) == ("interactive", 5)
