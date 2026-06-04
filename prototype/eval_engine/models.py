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
    # Prototype-only convenience: fixed output for the mock model so runs are deterministic
    # and need no API keys. Ignored for real models.
    mock_output: str | None = None
