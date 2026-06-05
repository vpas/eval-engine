"""Unit: RunSpec + registered-entity spec defaults / validation — no backends."""
import pytest
from pydantic import ValidationError

from eval_engine.models import DatasetSpec, EvalSpec, ModelSpec, PluginRef, RunSpec


def test_runspec_defaults():
    s = RunSpec(eval="e", dataset="d", harness=PluginRef(type="single_turn"), scorers=[])
    assert s.eval_version == 1 and s.epochs == 1 and s.batch_size == 50
    assert s.budget_usd is None and s.lane is None and s.team is None


def test_runspec_requires_harness():
    with pytest.raises(ValidationError):
        RunSpec(eval="e", dataset="d", scorers=[])  # type: ignore[call-arg]


def test_entity_specs_default_to_version_1():
    assert DatasetSpec(id="d", uri="x.jsonl").version == 1
    assert ModelSpec(id="m", provider="openrouter", model_id="x").version == 1
    e = EvalSpec(id="e", dataset="d", default_harness=PluginRef(type="single_turn"), default_scorers=[])
    assert e.version == 1 and e.dataset == "d"
