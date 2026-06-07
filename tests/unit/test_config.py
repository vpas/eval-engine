"""Unit: the shared cross-module tunables (``eval_engine.config``) — pure, no backends.

Why test a file of constants? Its whole reason to exist is that a default must NOT silently diverge
between the two modules that read it (the orchestrator that *enforces* the admission cap and the ops
dashboard that *displays* it; the orchestrator + monitor that share the stale-leader threshold). So we
pin the defaults, their types, and that each is overridable from its documented env var.
"""
from __future__ import annotations

import importlib

import pytest

from eval_engine import config


def test_defaults_match_the_documented_v1_envelope():
    assert config.GLOBAL_MAX_RUNNING == 50
    assert config.INTERACTIVE_RESERVE == 12          # ~25% of the cap
    assert config.INTERACTIVE_RESERVE < config.GLOBAL_MAX_RUNNING  # reserve is a slice OF the cap
    assert config.ORCH_TICK_SECONDS == 2.0
    assert config.WORKER_POLL_SECONDS == 1.0
    assert config.STALE_LEADER_SECONDS == 20.0


def test_value_types_are_what_readers_expect():
    # admission caps are counts (int); cadences/thresholds are seconds (float).
    assert isinstance(config.GLOBAL_MAX_RUNNING, int)
    assert isinstance(config.INTERACTIVE_RESERVE, int)
    assert isinstance(config.ORCH_TICK_SECONDS, float)
    assert isinstance(config.WORKER_POLL_SECONDS, float)
    assert isinstance(config.STALE_LEADER_SECONDS, float)


@pytest.mark.parametrize(
    "env, attr, raw, expected",
    [
        ("EVAL_ENGINE_GLOBAL_MAX_RUNNING", "GLOBAL_MAX_RUNNING", "200", 200),
        ("EVAL_ENGINE_INTERACTIVE_RESERVE", "INTERACTIVE_RESERVE", "40", 40),
        ("EVAL_ENGINE_ORCH_TICK", "ORCH_TICK_SECONDS", "0.5", 0.5),
        ("EVAL_ENGINE_WORKER_POLL", "WORKER_POLL_SECONDS", "0.25", 0.25),
        ("EVAL_ENGINE_STALE_LEADER_SECONDS", "STALE_LEADER_SECONDS", "45", 45.0),
    ],
)
def test_each_knob_is_overridable_from_its_env_var(monkeypatch, env, attr, raw, expected):
    monkeypatch.setenv(env, raw)
    reloaded = importlib.reload(config)
    try:
        assert getattr(reloaded, attr) == expected
    finally:
        monkeypatch.delenv(env, raising=False)
        importlib.reload(config)  # restore process-wide defaults for any later test
