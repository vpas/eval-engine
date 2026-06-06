"""The worker's graceful-drain flag: SIGTERM sets _STOP so the claim loop stops taking new work."""
from __future__ import annotations

from eval_engine import worker


def test_sigterm_sets_stop_flag():
    assert worker._STOP is False
    try:
        worker._graceful_shutdown()
        assert worker._STOP is True
    finally:
        worker._STOP = False  # reset module global for other tests


def test_runner_exposes_model_timeouts():
    from eval_engine import runner
    # hung-call protection is configured (env-tunable) so a stuck request can't wedge the worker
    assert runner.MODEL_TIMEOUT > 0 and runner.SAMPLE_TIME_LIMIT > 0
