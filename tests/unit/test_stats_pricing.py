"""Unit: the Wilson CI + cost pricing — pure functions, no backends."""
from eval_engine.runner import _cost_usd, _split_model, wilson_ci


def test_wilson_ci_known_value():
    lo, hi = wilson_ci(50, 100)
    assert (round(lo, 3), round(hi, 3)) == (0.404, 0.596)


def test_wilson_ci_clamps_to_unit_interval():
    assert wilson_ci(0, 0) == (0.0, 0.0)
    lo, hi = wilson_ci(10, 10)        # perfect score
    assert lo > 0.7 and hi == 1.0     # never exceeds 1
    lo, hi = wilson_ci(0, 10)         # zero score
    assert lo == 0.0 and hi < 0.31    # never below 0


def test_split_model():
    assert _split_model("openrouter/openai/gpt-4o-mini") == ("openrouter", "openai/gpt-4o-mini")
    assert _split_model("mockllm") == ("", "mockllm")


def test_cost_zero_when_unpriced(monkeypatch):
    # offline / unknown model → no catalog price → 0.0 (graceful, never fatal)
    monkeypatch.setattr("eval_engine.runner._openrouter_prices", lambda: {})
    assert _cost_usd("openai/gpt-4o-mini", 1000, 1000) == 0.0
    assert _cost_usd("mockllm/model", 10, 10) == 0.0


def test_cost_prices_gateway_and_direct_the_same(monkeypatch):
    # the LiteLLM gateway fronts OpenRouter, so openai/<id> is priced like openrouter/<id>
    monkeypatch.setattr("eval_engine.runner._openrouter_prices",
                        lambda: {"openai/gpt-4o-mini": (1e-6, 2e-6)})
    direct = _cost_usd("openrouter/openai/gpt-4o-mini", 1000, 500)
    gateway = _cost_usd("openai/openai/gpt-4o-mini", 1000, 500)
    assert direct == gateway == 1000 * 1e-6 + 500 * 2e-6
