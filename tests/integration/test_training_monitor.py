"""Integration: the full training-monitor spine end-to-end against real Postgres + ClickHouse, driven
by the fault-injecting mock trainer (no network — deterministic mockllm).

Proves: a scripted rising trajectory flows checkpoints → tagged Runs → score-vs-step series with CIs;
an injected regression + training-metric spike is flagged with a *training-fault* diagnosis; a healthy
canary eval stays clean; and the same-shaped dip WITHOUT corroboration reads as 'model-weak' — the
discriminator the whole subsystem exists for (docs/TRAINING_MONITOR.md §8).
"""
from __future__ import annotations

import uuid

import pytest

from eval_engine import training
from eval_engine.db import control
from eval_engine.mock_trainer import EvalCurve, MockTrainer, Scenario

pytestmark = pytest.mark.usefixtures("clean_db")

STEPS = [2000, 6000, 10000, 16000, 22000, 28000, 34000, 40000]


def _scenario(tr_id, *, with_spike: bool) -> Scenario:
    evals = [
        EvalCurve("capitals_qa", ceil=0.98, tau=4000, role="canary", n=60),   # canary: should stay ~100%
        EvalCurve("gsm8k_math", ceil=0.80, tau=20000, n=60),
        EvalCurve("humaneval_code", ceil=0.70, tau=26000, n=60),
    ]
    acc_faults = {(34000, "gsm8k_math"): 0.18}      # a sharp regression at step 34k
    metric_faults = {}
    if with_spike:                                   # corroborate it with a training-side spike
        metric_faults = {34000: {"loss": 0.13, "grad": 1.81, "throughput_mult": 0.87}}
    return Scenario(training_run_id=tr_id, model=f"atlas-{tr_id[:4]}", base="atlas-base",
                    steps=STEPS, evals=evals, planned_steps=120000,
                    acc_faults=acc_faults, metric_faults=metric_faults)


def _run(tmp_path, with_spike: bool) -> str:
    tr_id = "tr-" + uuid.uuid4().hex[:8]
    source = str(tmp_path / tr_id)
    mock = MockTrainer(source, _scenario(tr_id, with_spike=with_spike))
    mock.setup()
    mock.emit_all()
    mock.finish("completed")
    training.register_from_source(source)
    # one full pass: discover → fan out (inline-execute each checkpoint-eval) → reconcile → detect
    training.discover(tr_id)
    training.fan_out(tr_id, inline=True)
    training.reconcile(tr_id)
    return tr_id


def test_trajectory_and_training_fault_diagnosis(tmp_path):
    tr_id = _run(tmp_path, with_spike=True)

    # every (eval, step) scored, with a Wilson CI and a fitted expected value
    series = {s["eval_id"]: [] for s in control.checkpoint_scores(tr_id)}
    for s in control.checkpoint_scores(tr_id):
        series[s["eval_id"]].append(s)
    assert set(series) == {"capitals_qa", "gsm8k_math", "humaneval_code"}
    for ev, rows in series.items():
        rows.sort(key=lambda r: r["step"])
        assert len(rows) == len(STEPS)
        assert all(r["ci_lo"] <= r["accuracy"] <= r["ci_hi"] for r in rows)
        assert all(r["expected"] is not None for r in rows)

    # the canary rose to ~100% and never regresses
    canary = sorted(series["capitals_qa"], key=lambda r: r["step"])
    assert canary[-1]["accuracy"] >= 0.95

    # the injected regression is flagged on gsm8k_math at step 34k, with a TRAINING-FAULT diagnosis
    anoms = {a["eval"]: a for a in control.list_anomalies(tr_id)}
    assert "gsm8k_math" in anoms
    g = anoms["gsm8k_math"]
    assert g["kind"] == "regression" and g["step"] == 34000 and g["from"] == 28000
    assert g["diagnosis"] in ("training-divergence", "bad-checkpoint")
    assert g["delta"] < 0
    # the diagnosis is corroborated by the persisted signals + the regressed-sample drill data
    assert any(s["k"] == "grad-norm" and s["bad"] for s in g["signals"])
    assert g["samples"], "expected regressed samples (passed before, fail now)"
    # the canary did NOT raise an anomaly
    assert "capitals_qa" not in anoms

    # best checkpoint per eval is recorded (the ship-candidate summary)
    best = training.best_checkpoints(tr_id)
    assert set(best) == {"capitals_qa", "gsm8k_math", "humaneval_code"}


def test_same_dip_without_corroboration_reads_as_model_weak(tmp_path):
    tr_id = _run(tmp_path, with_spike=False)
    anoms = {a["eval"]: a for a in control.list_anomalies(tr_id)}
    assert "gsm8k_math" in anoms
    # identical dip, but no training-side spike ⇒ diagnosed as the model simply being weak here
    assert anoms["gsm8k_math"]["diagnosis"] == "model-weak-on-eval"


def test_checkpoint_runs_are_tagged_and_resolve_to_a_real_model(tmp_path):
    tr_id = _run(tmp_path, with_spike=True)
    # the checkpoint-eval runs carry the training provenance + sweep, and the opaque checkpoint model_ref
    runs = control.runs_for_checkpoint(f"ck-{tr_id}-34000")
    assert {r["eval_id"] for r in runs} == {"capitals_qa", "gsm8k_math", "humaneval_code"}
    assert all(r["status"] == "completed" for r in runs)
    # the resolver mapped the checkpoint ref → a real (mock) model behind the scenes
    assert control.get_checkpoint_model(f"checkpoint:{tr_id}:34000")["real_model"] == "mockllm/model"


def test_cadence_skips_checkpoints(tmp_path):
    tr_id = "tr-" + uuid.uuid4().hex[:8]
    source = str(tmp_path / tr_id)
    sc = _scenario(tr_id, with_spike=False)
    sc.config = {"eval_every": 2}                    # eval every other checkpoint
    mock = MockTrainer(source, sc)
    mock.setup(); mock.emit_all(); mock.finish("completed")
    training.register_from_source(source)
    training.discover(tr_id)
    training.fan_out(tr_id, inline=True)
    ckpts = {c["step"]: c["status"] for c in control.list_checkpoints(tr_id)}
    assert ckpts[2000] == "evaluating"   # idx 0 → evaluated
    assert ckpts[6000] == "skipped"      # idx 1 → skipped
    assert ckpts[10000] == "evaluating"  # idx 2 → evaluated
