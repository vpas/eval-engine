"""Unit: the centralized logging setup (``eval_engine.logs``) — pure, no backends.

The one subtlety worth pinning: the worker/orchestrator/monitor run as ``python -m eval_engine.<mod>``,
so their ``__name__`` is ``"__main__"`` — NOT a child of the ``eval_engine`` parent logger, so without
the remap their lines would fall through to ``logging.lastResort`` (unformatted, WARNING-only) and never
reach the configured stdout handler the log viewer reads. ``get_logger`` remaps ``__main__`` back to its
real dotted name so those entrypoints log through the same handler as every imported module.
"""
from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

import pytest

from eval_engine import logs

_PARENT = "eval_engine"


@pytest.fixture
def restore_logging():
    """Snapshot + restore the parent logger's handlers/level/flag so a test can't leak logging state."""
    parent = logging.getLogger(_PARENT)
    handlers, level, propagate, configured = (
        list(parent.handlers), parent.level, parent.propagate, logs._configured)
    yield parent
    parent.handlers[:] = handlers
    parent.setLevel(level)
    parent.propagate = propagate
    logs._configured = configured


def test_get_logger_remaps_main_to_dotted_name(monkeypatch):
    # `python -m eval_engine.worker` → __name__=="__main__" but __spec__.name is the real dotted path.
    fake_main = SimpleNamespace(__spec__=SimpleNamespace(name="eval_engine.worker"))
    monkeypatch.setitem(sys.modules, "__main__", fake_main)
    assert logs.get_logger("__main__").name == "eval_engine.worker"


def test_get_logger_main_without_spec_stays_main(monkeypatch):
    # No __spec__ (e.g. a bare `python script.py`) → nothing to remap to, leave the name as-is.
    monkeypatch.setitem(sys.modules, "__main__", SimpleNamespace(__spec__=None))
    assert logs.get_logger("__main__").name == "__main__"


def test_get_logger_passes_dotted_names_through():
    assert logs.get_logger("eval_engine.runner").name == "eval_engine.runner"


def test_setup_installs_exactly_one_handler_and_is_idempotent(restore_logging):
    parent = restore_logging
    # Reset to a pristine state, then call setup repeatedly — the handler must be installed once only.
    parent.handlers[:] = []
    logs._configured = False
    logs.setup()
    logs.setup()
    logs.setup()
    ours = [h for h in parent.handlers if isinstance(h, logging.StreamHandler)]
    assert len(ours) == 1
    # We never propagate to the Python root — uvicorn owns that, double-emit would duplicate every line.
    assert parent.propagate is False


def test_setup_level_from_argument_and_env(restore_logging, monkeypatch):
    parent = restore_logging
    logs.setup(level="DEBUG")
    assert parent.level == logging.DEBUG

    monkeypatch.setenv("EVAL_ENGINE_LOG_LEVEL", "warning")  # case-insensitive
    logs.setup()  # no explicit arg → read the env
    assert parent.level == logging.WARNING


def test_unknown_level_falls_back_to_info(restore_logging):
    logs.setup(level="NOPE")
    assert restore_logging.level == logging.INFO


def test_child_loggers_inherit_the_parent_handler(restore_logging):
    # A module logger is a child of `eval_engine`, so it has no handler of its own but reaches ours.
    parent = restore_logging
    parent.handlers[:] = []
    logs._configured = False
    logs.setup()
    child = logs.get_logger("eval_engine.api")
    assert child.handlers == [] and child.parent is not None
    # The effective handler chain resolves to the single parent handler.
    assert any(h in parent.handlers for h in logging.getLogger("eval_engine.api").parent.handlers)
