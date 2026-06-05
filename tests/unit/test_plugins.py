"""Unit: the plugin registry + catalog — no backends."""
import pytest

from eval_engine import builtins, plugins  # noqa: F401  importing builtins populates the registry


def test_catalog_spans_the_eval_shapes():
    cat = {(p["kind"], p["name"]) for p in plugins.catalog()}
    assert {("harness", "single_turn"), ("harness", "multiple_choice"), ("harness", "agentic")} <= cat
    assert {("scorer", "includes"), ("scorer", "choice"), ("scorer", "llm_judge")} <= cat


def test_build_resolves_and_instantiates():
    solver, plugin = plugins.build("harness", {"type": "single_turn"})
    assert plugin.name == "single_turn" and solver is not None


def test_get_unknown_lists_available():
    with pytest.raises(KeyError) as exc:
        plugins.get("harness", "does_not_exist", "1.0.0")
    assert "available" in str(exc.value)


def test_catalog_exposes_config_json_schema():
    agentic = next(p for p in plugins.catalog() if p["name"] == "agentic")
    assert "properties" in agentic["config_schema"]  # the launch-wizard form is derived from this
