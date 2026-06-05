"""RunSpec — the reproducible unit (prototype of SCHEMA §1.5)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class PluginRef(BaseModel):
    type: str
    version: str = "1.0.0"
    config: dict = Field(default_factory=dict)


class RunSpec(BaseModel):
    eval: str
    dataset: str
    model: str = "mockllm/model"
    harness: PluginRef
    scorers: list[PluginRef]
    limit: int | None = None
    batch_size: int = 50  # ledger claim batch (worker grabs this many sample-tasks at a time)
    # Sampling for statistical comparability (DESIGN §14, FR8). epochs = repeat each sample N times
    # (Inspect reduces to a per-sample score) → stabler scores + a basis for variance/CIs. Outputs are
    # comparable, not bitwise (hosted models are non-deterministic even at temperature 0 / fixed seed).
    epochs: int = 1
    temperature: float | None = None
    seed: int | None = None
    # Cost cap for the whole run (USD). When committed cost reaches it, remaining queued samples are
    # marked terminal `budget_skipped` (a DISTINCT terminal class — not `failed`, so it neither burns
    # retries nor inflates failed_samples; DESIGN §8). None = uncapped. The canonical form is the
    # gateway's own per-run_id reject (deferred "A5"); this enforces the same semantics control-side.
    budget_usd: float | None = None
    # Prototype-only convenience: fixed output for the mock model so runs are deterministic
    # and need no API keys. Ignored for real models.
    mock_output: str | None = None
    # Prototype-only convenience: a scripted tool-call sequence ([{tool, args}, ...]) for a mock
    # AGENTIC run — deterministic agent trajectory with no provider key. Ignored for real models.
    mock_tool_calls: list[dict] | None = None
