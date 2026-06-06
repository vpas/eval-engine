"""Mock trainer (docs/TRAINING_MONITOR.md §4) — there is no real trainer (the v1 non-goal), so this
stand-in *fakes* the checkpoint stream and the per-checkpoint serving.

It writes the manifest contract the monitor's poller reads (``run.json`` + an ``index.jsonl`` the
monitor diffs), and maintains the checkpoint→model resolver. Two jobs:

  - **A scripted trajectory.** Each eval rises along a saturating curve ``ceil·(1−e^(−step/τ))``; the
    underlying "model" is a deterministic ``mockllm`` whose output is a per-checkpoint *answer key* that
    yields exactly the target accuracy (no network / API key). So a checkpoint's accuracy on each eval
    is controllable to the sample — which is how we…
  - **…inject faults.** A fault subtracts from an eval's accuracy at chosen steps (a cliff, a slow
    drift, a plateau) and can perturb the training metrics (a loss/grad-norm spike, a throughput
    stall) — each engineered to trip a specific §8 detector + diagnosis. So this module doubles as the
    integration-test fixture.

The accuracy mechanism: each eval's samples have a globally-unique fixed-width target token
(``<eval>-000``…); the answer key is the space-joined set of tokens that should *pass* at a checkpoint,
and the ``includes`` scorer passes a sample iff its token is in the key — so accuracy = ⌈a·N⌉ / N.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import datasets as ds_mod, storage
from .db import control
from .models import DatasetSpec, EvalSpec, PluginRef, SuiteEntry, TrainingRunSpec

_MOCK_DATA = Path(os.environ.get("EVAL_ENGINE_DATA", ".data")) / "training_mock"


@dataclass
class EvalCurve:
    id: str
    ceil: float
    tau: float
    role: str = "standard"
    color: str | None = None
    n: int = 8                         # samples in this eval's (mock) dataset


@dataclass
class Scenario:
    """A scripted training run: its evals' clean trajectories + injected faults."""
    training_run_id: str
    model: str
    steps: list[int]
    evals: list[EvalCurve]
    base: str = ""
    planned_steps: int = 0
    # faults
    acc_faults: dict = field(default_factory=dict)     # {(step, eval_id): delta_subtracted}
    metric_faults: dict = field(default_factory=dict)  # {step: {"loss":+d,"grad":g,"throughput_mult":m}}
    config: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- registration

def _register_eval(ev: EvalCurve) -> None:
    """Write + register this eval's mock dataset (unique target tokens) and its EvalSpec."""
    _MOCK_DATA.mkdir(parents=True, exist_ok=True)
    path = _MOCK_DATA / f"{ev.id}.jsonl"
    lines = []
    for i in range(ev.n):
        tok = f"{ev.id}-{i:03d}"
        lines.append(json.dumps({"id": tok, "input": f"emit {tok}", "target": tok,
                                  "metadata": {"category": f"cat{i % 3}"}}))
    path.write_text("\n".join(lines) + "\n")

    ds_id = f"{ev.id}_ds"
    content_hash, snapshot_uri = ds_mod.snapshot(str(path))
    ds = DatasetSpec(id=ds_id, source="jsonl", uri=str(path),
                     content_hash=content_hash, snapshot_uri=snapshot_uri)
    control.register_entity("dataset", ds_id, 1, ds.model_dump())
    spec = EvalSpec(id=ev.id, dataset=ds_id, default_harness=PluginRef(type="single_turn"),
                    default_scorers=[PluginRef(type="includes", config={"ignore_case": True})])
    control.register_entity("eval", ev.id, 1, spec.model_dump())


def _answer_key(sc: Scenario, step: int) -> tuple[str, dict[str, float]]:
    """The mockllm answer key for a checkpoint = the passing tokens across all evals, sized to each
    eval's target accuracy. Returns (answer_key_string, {eval_id: accuracy})."""
    toks: list[str] = []
    accs: dict[str, float] = {}
    for ev in sc.evals:
        a = ev.ceil * (1 - math.exp(-step / ev.tau))
        a -= sc.acc_faults.get((step, ev.id), 0.0)
        a = max(0.0, min(1.0, a))
        k = round(a * ev.n)
        accs[ev.id] = k / ev.n
        toks += [f"{ev.id}-{i:03d}" for i in range(k)]
    return " ".join(toks), accs


def _train_metrics(sc: Scenario, step: int) -> dict:
    loss = 1.6 + 1.05 * math.exp(-step / 21000)
    grad = 0.38 + math.sin(step * 0.0009) * 0.04
    lr = 3e-4 * max(0.1, 1 - step / (sc.planned_steps or 120000))
    tp = 2950.0
    f = sc.metric_faults.get(step, {})
    loss += f.get("loss", 0.0)
    grad = f.get("grad", grad)
    tp *= f.get("throughput_mult", 1.0)
    return {"loss": round(loss, 4), "grad": round(grad, 4), "lr": lr, "throughput": round(tp, 1)}


# --------------------------------------------------------------------------- the trainer

class MockTrainer:
    def __init__(self, source: str, scenario: Scenario):
        self.source = source.rstrip("/")
        self.sc = scenario

    def _spec(self) -> TrainingRunSpec:
        return TrainingRunSpec(
            id=self.sc.training_run_id, model=self.sc.model, base=self.sc.base,
            planned_steps=self.sc.planned_steps or (self.sc.steps[-1] if self.sc.steps else 0),
            source=self.source,
            suite=[SuiteEntry(eval=e.id, role=e.role, color=e.color) for e in self.sc.evals],
            config=self.sc.config,
        )

    def setup(self) -> TrainingRunSpec:
        """Register the suite's evals/datasets and write run.json (status=training)."""
        for ev in self.sc.evals:
            _register_eval(ev)
        spec = self._spec()
        body = spec.model_dump()
        body["status"] = "training"
        storage.write_text(f"{self.source}/run.json", json.dumps(body))
        # start an empty index
        if not storage.exists(f"{self.source}/index.jsonl"):
            storage.write_text(f"{self.source}/index.jsonl", "")
        return spec

    def emit(self, step: int) -> dict:
        """Emit one checkpoint: register its resolver mapping + append its manifest to index.jsonl."""
        model_ref = f"checkpoint:{self.sc.training_run_id}:{step}"
        key, _accs = _answer_key(self.sc, step)
        control.set_checkpoint_model(model_ref, "mockllm/model", mock_output=key)
        manifest = {"training_run_id": self.sc.training_run_id, "step": step, "model_ref": model_ref,
                    "tokens": step * 2_100_000, "train_metrics": _train_metrics(self.sc, step)}
        existing = storage.read_text(f"{self.source}/index.jsonl") if storage.exists(f"{self.source}/index.jsonl") else ""
        storage.write_text(f"{self.source}/index.jsonl", existing + json.dumps(manifest) + "\n")
        return manifest

    def emit_all(self) -> None:
        for step in self.sc.steps:
            self.emit(step)

    def finish(self, status: str = "completed") -> None:
        body = self._spec().model_dump()
        body["status"] = status
        storage.write_text(f"{self.source}/run.json", json.dumps(body))
