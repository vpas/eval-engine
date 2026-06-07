"""Unit: runner helpers with branching logic — pure, no backends.

(`_split_model`, `_cost_usd`, `wilson_ci` live in test_stats_pricing.py; `_classify` in test_classify.py.)
"""
from inspect_ai.model import Model
from inspect_ai.scorer import CORRECT

from eval_engine.models import PluginRef, RunSpec
from eval_engine.runner import _model_for, _score_value


def test_score_value_correct_and_truthy():
    # Inspect's CORRECT sentinel, its "C" string, and 1/1.0/True all map to a full pass.
    for v in (CORRECT, "C", 1, 1.0, True):
        assert _score_value(v) == 1.0, v


def test_score_value_incorrect_and_nonnumeric():
    # "I"/0/False/None and any non-numeric junk map to 0.0 (never raise).
    for v in ("I", 0, False, None, "garbage", ""):
        assert _score_value(v) == 0.0, v


def test_score_value_numeric_passthrough():
    assert _score_value(0.75) == 0.75      # partial credit preserved
    assert _score_value(2) == 2.0          # ints coerced to float


def _spec(**over) -> RunSpec:
    base = dict(eval="e", dataset="d", model="mockllm/model",
                harness=PluginRef(type="single_turn"), scorers=[])
    base.update(over)
    return RunSpec(**base)


def test_model_for_mock_output_builds_offline():
    # mockllm + mock_output → a scripted model, no provider key / no network.
    m, exec_model = _model_for(_spec(mock_output="Paris"), n=3)
    assert isinstance(m, Model) and exec_model == "mockllm/model"


def test_model_for_mock_tool_calls_builds_offline():
    # the scripted-agentic branch: a tool-call sequence becomes the mock's outputs.
    spec = _spec(mock_tool_calls=[{"tool": "bash", "args": {"command": "ls"}},
                                  {"tool": "submit", "args": {"answer": "x"}}])
    m, _ = _model_for(spec, n=1)
    assert isinstance(m, Model)
