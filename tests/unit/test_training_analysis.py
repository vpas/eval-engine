"""Unit tests for the training-monitor analysis core (no backends): expected-curve fit, the
CI-aware regression/drift/plateau detectors, cadence helpers, and the diagnosis rule-combiner."""
from __future__ import annotations

import math

from eval_engine import training_analysis as ta


# --------------------------------------------------------------------------- expected curve

def _curve(ceil, tau, steps):
    return [(s, ceil * (1 - math.exp(-s / tau))) for s in steps]


def test_fit_recovers_a_saturating_curve():
    steps = [2000, 6000, 10000, 16000, 22000, 28000, 34000, 40000]
    pts = _curve(0.8, 20000, steps)
    f = ta.fit_expected(pts)
    # fit should track the generating curve closely at every observed step
    for s, a in pts:
        assert abs(f(s) - a) < 0.03
    # and stay in [0,1], monotonic increasing
    assert 0 <= f(2000) < f(40000) <= 1


def test_fit_cold_start_under_three_points_is_silent():
    f = ta.fit_expected([(2000, 0.5), (6000, 0.6)])
    # expected == last observed for unknown steps → deviation ~0, nothing to flag yet
    assert f(6000) == 0.6
    assert f(99999) == 0.6


# --------------------------------------------------------------------------- cadence

def test_should_eval_every_kth():
    assert [ta.should_eval(i, 3) for i in range(6)] == [True, False, False, True, False, False]
    assert all(ta.should_eval(i, 1) for i in range(5))  # every checkpoint


def test_sample_limit_milestone_is_full_size():
    # non-milestone uses the subsample; milestone (idx % every == 0) runs full (None)
    assert ta.sample_limit_for(1, 200, 4, None) == 200
    assert ta.sample_limit_for(4, 200, 4, None) is None
    # per-eval override wins over the run default
    assert ta.sample_limit_for(1, 200, 4, 50) == 50


# --------------------------------------------------------------------------- detectors

def _series(steps, accs, ceil=0.8, tau=20000, ci=0.03):
    f = ta.fit_expected(list(zip(steps, accs)))
    return [{"step": s, "accuracy": a, "ci_lo": a - ci, "ci_hi": a + ci, "expected": f(s)}
            for s, a in zip(steps, accs)]


def test_regression_flags_a_ci_clearing_cliff():
    steps = [2000, 6000, 10000, 16000, 22000, 28000]
    accs = [0.30, 0.52, 0.64, 0.71, 0.74, 0.55]  # cliff at the last step
    a = ta.detect_eval_anomaly(_series(steps, accs, ci=0.02), threshold=0.015)
    assert a and a["kind"] == "regression"
    assert a["step"] == 28000 and a["from"] == 22000
    assert a["delta"] < 0 and a["severity"] in ("high", "medium")


def test_no_anomaly_when_drop_is_within_the_ci_noise_band():
    steps = [2000, 6000, 10000, 16000, 22000, 28000]
    accs = [0.30, 0.52, 0.64, 0.71, 0.74, 0.72]  # a 2pp wobble
    # wide CIs (±8pp) ⇒ the dip never clears the noise floor ⇒ not flagged
    a = ta.detect_eval_anomaly(_series(steps, accs, ci=0.08), threshold=0.015)
    assert a is None


def test_plateau_detected_when_climb_stalls_into_a_flat_tail():
    steps = [2000, 6000, 10000, 16000, 22000, 28000]
    accs = [0.20, 0.45, 0.60, 0.66, 0.665, 0.667]  # climbs steeply, then flattens at ~0.66
    a = ta.detect_eval_anomaly(_series(steps, accs, ci=0.02), threshold=0.015)
    assert a and a["kind"] == "plateau"
    assert a["step"] == 28000 and a["severity"] in ("low", "medium")


def test_drift_detected_on_a_gentle_sustained_decline():
    # every per-step drop is < threshold (so it's NOT a regression), but it erodes steadily
    steps = [2000, 6000, 10000, 16000, 22000, 28000]
    accs = [0.96, 0.96, 0.96, 0.948, 0.936, 0.924]  # peak at 10k then −1.2pp/step
    a = ta.detect_eval_anomaly(_series(steps, accs, ci=0.02), threshold=0.015)
    assert a and a["kind"] == "drift"
    assert a["from"] == 10000 and a["span"] >= 3


def test_sharp_multistep_decline_reads_as_regression_not_drift():
    steps = [2000, 6000, 10000, 16000, 22000, 28000]
    accs = [0.90, 0.92, 0.93, 0.89, 0.85, 0.81]  # 4pp/step — each step is itself a cliff
    a = ta.detect_eval_anomaly(_series(steps, accs, ci=0.02), threshold=0.015)
    assert a and a["kind"] == "regression"


# --------------------------------------------------------------------------- diagnosis

def test_diagnose_serving_error_when_error_rate_high():
    label, cause = ta.diagnose("regression", canary_collapsed=False, error_rate=0.5, breadth=1,
                               loss_delta=0.0, grad=0.4, throughput_drop=0.0, capability_rising=False)
    assert label == "serving/infra-error"


def test_diagnose_bad_checkpoint_on_canary_collapse():
    label, _ = ta.diagnose("regression", canary_collapsed=True, error_rate=0.0, breadth=1,
                           loss_delta=0.0, grad=0.4, throughput_drop=0.0, capability_rising=False)
    assert label == "bad-checkpoint"


def test_diagnose_corrupted_shard_on_broad_drop_with_loss_spike():
    label, _ = ta.diagnose("regression", canary_collapsed=False, error_rate=0.0, breadth=4,
                           loss_delta=0.13, grad=1.81, throughput_drop=0.13, capability_rising=False)
    assert label == "bad-checkpoint"


def test_diagnose_training_fault_on_isolated_dip_with_gradnorm_spike():
    # the discriminator: an ISOLATED eval dip (breadth 1) but a grad-norm blow-up ⇒ training fault,
    # NOT model-weak (same dip without the spike ⇒ model-weak, asserted below)
    label, _ = ta.diagnose("regression", canary_collapsed=False, error_rate=0.0, breadth=1,
                           loss_delta=0.06, grad=1.81, throughput_drop=0.13, capability_rising=False)
    assert label == "training-divergence"


def test_diagnose_alignment_drift_when_capability_rising():
    label, _ = ta.diagnose("drift", canary_collapsed=False, error_rate=0.0, breadth=1,
                           loss_delta=-0.02, grad=0.4, throughput_drop=0.0, capability_rising=True)
    assert label == "alignment-drift"


def test_diagnose_model_weak_when_isolated_and_uncorroborated():
    label, _ = ta.diagnose("regression", canary_collapsed=False, error_rate=0.0, breadth=1,
                           loss_delta=0.0, grad=0.4, throughput_drop=0.0, capability_rising=False)
    assert label == "model-weak-on-eval"
