"""Training-monitor analysis — pure functions (no I/O), so they unit-test without backends.

Two jobs (docs/TRAINING_MONITOR.md §8):
  - **Expected trajectory.** Fit a *simple* saturating curve ``expected(step) = ceil·(1−e^(−step/τ))``
    per eval (the "middle path": cheap enough to ship in v1, powers the band + the deviation metric;
    the heavy GP/isotonic version stays deferred). Cold-start (<3 points) falls back to "expected =
    last observed", so the band is undefined and nothing is flagged until there's a trajectory.
  - **Anomaly detection + diagnosis.** CI-aware ``regression`` / ``drift`` / ``plateau`` detectors over
    one eval's score series, collapsed to the single most-significant anomaly; then a rule-combiner over
    corroborating signals (training metrics, breadth, canary, error-rate) yields a diagnosis label +
    a templated root-cause sentence that separates a *training fault* from *a model simply weak here*.

Everything here takes plain data (lists of dicts / tuples) and returns plain data.
"""
from __future__ import annotations

import math
from typing import Callable


# --------------------------------------------------------------------------- expected curve

def fit_expected(points: list[tuple[float, float]]) -> Callable[[float], float]:
    """Fit ``expected(step) = ceil·(1−e^(−step/τ))`` to ``points = [(step, accuracy), …]``.

    Numpy-free: grid-search τ; for each τ the optimal ``ceil`` is closed-form least squares
    (``ceil = Σ a·f / Σ f²`` with ``f = 1−e^(−step/τ)``), pick the τ with the smallest residual.
    Robust to the injected dips (they raise the residual but don't dominate the fit). With <3 points
    there isn't a trajectory to fit yet, so ``expected`` is just the last observed accuracy — the band
    is undefined and the detectors stay silent (cold-start)."""
    pts = sorted(p for p in points if p[1] is not None)
    if len(pts) < 3:
        last = pts[-1][1] if pts else 0.0
        table = {float(s): a for s, a in pts}
        return lambda s, _t=table, _l=last: _t.get(float(s), _l)

    steps = [float(s) for s, _ in pts]
    accs = [float(a) for _, a in pts]
    best: tuple[float, float, float] | None = None  # (residual, ceil, tau)
    # τ grid spans a fast saturation (~2k steps) to a very slow one (~250k) — geometric so it's dense
    # where the curvature lives.
    taus = [2000.0 * (1.3 ** k) for k in range(0, 23)]
    for tau in taus:
        f = [1.0 - math.exp(-s / tau) for s in steps]
        sff = sum(v * v for v in f) or 1e-9
        ceil = sum(a * v for a, v in zip(accs, f)) / sff
        ceil = max(0.05, min(1.0, ceil))
        resid = sum((a - ceil * v) ** 2 for a, v in zip(accs, f))
        if best is None or resid < best[0]:
            best = (resid, ceil, tau)
    _, ceil, tau = best  # type: ignore[misc]
    return lambda s, _c=ceil, _t=tau: _c * (1.0 - math.exp(-float(s) / _t))


# --------------------------------------------------------------------------- cadence (cost vs frequency)

def should_eval(idx: int, eval_every: int) -> bool:
    """Evaluate every ``eval_every``-th discovered checkpoint (idx is 0-based discovery order)."""
    every = max(1, int(eval_every or 1))
    return idx % every == 0


def is_milestone(idx: int, milestone_every: int | None) -> bool:
    """A milestone checkpoint runs the FULL suite at full dataset size (overrides per-checkpoint
    subsampling). ``None`` ⇒ every evaluated checkpoint is full-size."""
    if not milestone_every:
        return True
    return idx % int(milestone_every) == 0


def sample_limit_for(idx: int, cfg_sample_limit: int | None, milestone_every: int | None,
                     entry_sample_limit: int | None) -> int | None:
    """Per-checkpoint dataset subsample size (cost vs cadence). A per-eval override wins; else the run
    default; but a milestone checkpoint always runs full-size (returns None = no limit)."""
    if is_milestone(idx, milestone_every):
        return None
    return entry_sample_limit if entry_sample_limit is not None else cfg_sample_limit


# --------------------------------------------------------------------------- anomaly detection

# A "drop" must clear BOTH the configured threshold (pp/100) AND the statistical noise floor — the
# Wilson CI. We only flag when the checkpoint's CI upper bound sits below the expected value, so we
# never fire on sampling noise (docs/TRAINING_MONITOR.md §8, reuse of the Wilson CI from gap #4).

def _severity(kind: str, delta: float, span: int) -> str:
    mag = abs(delta)
    if kind == "plateau":                       # a stall is informative, not alarming (prototype: low)
        return "medium" if mag >= 0.06 else "low"
    if mag >= 0.08 or (mag >= 0.05 and span >= 3):
        return "high"
    if mag >= 0.03:
        return "medium"
    return "low"


def detect_eval_anomaly(series: list[dict], threshold: float) -> dict | None:
    """Most-significant anomaly for ONE eval's score series, or None.

    ``series`` items (sorted by step): ``{step, accuracy, ci_lo, ci_hi, expected}``. ``threshold`` is a
    fraction (e.g. 0.015 for 1.5pp). Returns one of ``regression`` | ``drift`` | ``plateau`` with
    ``{step, from, kind, delta, severity, span}``, collapsed to the single worst so a sustained dip is
    one anomaly, not one per step. The three kinds are mutually exclusive by construction:

      - ``regression`` — a *sharp* single-step cliff (the step itself drops > threshold), CI-clearing,
        below the expected band.
      - ``drift``      — a *gentle* sustained decline (no single step exceeds threshold, so regression
        misses it) whose cumulative drop ≥ threshold and which ends below expected.
      - ``plateau``    — improvement stalled: the tail is flat while the run was still climbing into it.
    """
    s = [r for r in series if r.get("accuracy") is not None]
    if len(s) < 2:
        return None
    cands: list[dict] = []

    # regression: a CI-clearing SHARP step-drop (the single step exceeds threshold) below expected.
    for i in range(1, len(s)):
        cur, prev = s[i], s[i - 1]
        exp = cur.get("expected", cur["accuracy"])
        below_expected = exp - cur["accuracy"] > threshold
        ci_clears = cur.get("ci_hi", cur["accuracy"]) < exp - 1e-9   # noise gate: CI doesn't reach expected
        sharp_drop = cur["accuracy"] < prev["accuracy"] - threshold
        if below_expected and ci_clears and sharp_drop:
            cands.append({"kind": "regression", "step": cur["step"], "from": prev["step"],
                          "delta": cur["accuracy"] - exp, "span": 1})

    # drift: a GENTLE non-increasing run ending at the last step — every per-step drop is < threshold
    # (else it's a regression), but the cumulative drop ≥ threshold and it ends below expected.
    j = len(s) - 1
    start = j
    while start > 0 and s[start]["accuracy"] <= s[start - 1]["accuracy"] + 1e-9:
        start -= 1
    # anchor `from` at the LATEST peak in the run (a flat top shouldn't push the baseline back to step 0)
    run_peak = max(r["accuracy"] for r in s[start:j + 1])
    peak_idx = max(k for k in range(start, j + 1) if s[k]["accuracy"] >= run_peak - 1e-9)
    span = j - peak_idx
    gentle = all(s[k - 1]["accuracy"] - s[k]["accuracy"] < threshold for k in range(peak_idx + 1, j + 1))
    if span >= 2 and gentle:
        last = s[j]["accuracy"]
        exp = s[j].get("expected", last)
        if run_peak - last >= threshold and exp - last > threshold / 2:
            cands.append({"kind": "drift", "step": s[j]["step"], "from": s[peak_idx]["step"],
                          "delta": last - exp, "span": span})

    # plateau: the last 3 steps are flat (range ≤ threshold) but the run was still climbing right into
    # them (the step into the flat region rose ≥ threshold) — improvement stalled earlier than the
    # trajectory implied. Defined structurally (not vs the fit, which would just absorb the plateau).
    if len(s) >= 4:
        tail = s[-3:]
        accs = [r["accuracy"] for r in tail]
        flat = max(accs) - min(accs) <= threshold
        pre_rise = s[-3]["accuracy"] - s[-4]["accuracy"]   # the climb into the flat tail
        if flat and pre_rise >= threshold:
            cands.append({"kind": "plateau", "step": tail[-1]["step"], "from": tail[0]["step"],
                          "delta": -pre_rise, "span": len(tail)})

    if not cands:
        return None
    # worst by |delta|, regression preferred on ties (sharpest signal).
    rank = {"regression": 0, "drift": 1, "plateau": 2}
    cands.sort(key=lambda c: (-abs(c["delta"]), rank[c["kind"]]))
    best = cands[0]
    best["severity"] = _severity(best["kind"], best["delta"], best["span"])
    return best


# --------------------------------------------------------------------------- diagnosis (fault vs weak)

def diagnose(kind: str, *, canary_collapsed: bool, error_rate: float, breadth: int,
             loss_delta: float | None, grad: float | None, throughput_drop: float | None,
             capability_rising: bool) -> tuple[str, str]:
    """Rule-combiner over corroborating signals → (diagnosis_label, root-cause sentence). The point is
    to separate a TRAINING/serving fault (broad, sudden, garbage/errors, training-metric corroborated)
    from a model that is simply WEAK on a hard eval (isolated, stable, no corroboration)."""
    if error_rate >= 0.2:
        return ("serving/infra-error",
                f"Sample errors spiked to {error_rate*100:.0f}% at this checkpoint — the serving endpoint "
                "or eval harness failed, rather than the model regressing.")
    if canary_collapsed:
        return ("bad-checkpoint",
                "A canary sanity eval collapsed — any functional model should ace it, so the checkpoint "
                "or its serving is broken rather than the model being genuinely weak.")
    if breadth >= 3 and ((loss_delta or 0) > 0.05 or (grad or 0) > 1.0):
        return ("bad-checkpoint",
                "Sharp regression across multiple evals coincident with a training-loss / grad-norm spike "
                "— most consistent with a corrupted data shard or an LR-scheduler event.")
    # A strong training-side signal corroborates a fault even on a SINGLE eval — a grad-norm blow-up or a
    # loss spike is training-side, not a model being weak at one task (the key discriminator).
    if (loss_delta or 0) > 0.08 or (grad or 0) > 1.5:
        return ("training-divergence",
                "Eval score fell alongside a training-loss / grad-norm spike — a training-side fault "
                "(optimization diverging or a bad update), not the model being weak at this eval.")
    if kind == "drift" and capability_rising:
        return ("alignment-drift",
                "A slow monotonic decline while raw capability keeps rising — the classic alignment-tax / "
                "safety-erosion pattern.")
    if kind == "plateau":
        return ("stuck/plateau",
                "Accuracy has plateaued across several checkpoints while the expected trajectory still "
                "climbs — likely a data-mix gap for this capability.")
    if kind == "drift":
        return ("overfitting",
                "A sustained decline below the expected trajectory — possible overfitting or a "
                "train/eval mismatch.")
    if breadth <= 1:
        return ("model-weak-on-eval",
                "An isolated dip on a single eval with no corroborating training-side or serving signal "
                "— most likely the model is simply weak here, near trajectory noise.")
    return ("regression",
            "A significant drop below the expected trajectory; the cause is indeterminate from the "
            "available signals.")


def build_signals(*, loss_delta: float | None, grad: float | None, throughput_drop: float | None,
                  lr_changed: bool, error_rate: float, breadth: int) -> list[dict]:
    """The correlated-signal cards the UI shows (k / value / note / bad) — the diagnosis evidence."""
    sig: list[dict] = []
    if loss_delta is not None:
        sig.append({"k": "train loss", "v": f"{loss_delta:+.2f}", "note": "vs baseline ckpt",
                    "bad": loss_delta > 0.03})
    if grad is not None:
        sig.append({"k": "grad-norm", "v": f"{grad:.2f}", "note": "spike" if grad > 1.0 else "nominal",
                    "bad": grad > 1.0})
    if throughput_drop is not None:
        sig.append({"k": "throughput", "v": f"{-throughput_drop*100:.0f}%",
                    "note": "shard stall" if throughput_drop > 0.05 else "steady",
                    "bad": throughput_drop > 0.05})
    sig.append({"k": "lr", "v": "changed" if lr_changed else "stable",
                "note": "scheduler event" if lr_changed else "no scheduler event", "bad": False})
    sig.append({"k": "sample errors", "v": f"{error_rate*100:.0f}%",
                "note": "serving/harness" if error_rate >= 0.2 else "clean", "bad": error_rate >= 0.2})
    sig.append({"k": "breadth", "v": f"{breadth} eval{'s' if breadth != 1 else ''}",
                "note": "broad collapse" if breadth >= 3 else "isolated", "bad": breadth >= 3})
    return sig
