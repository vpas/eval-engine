"""RunSpec — the reproducible unit (prototype of SCHEMA §1.5)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class PluginRef(BaseModel):
    type: str
    version: str = "1.0.0"
    config: dict = Field(default_factory=dict)


# --- Registered entities (DESIGN §7, FR1–3). Kept orthogonal so any valid combination composes into
# a RunSpec. Versions are immutable — re-registering an id mints a new version (the content-addressed,
# reproducible stance of §13/§14), so there is no in-place update/delete, only register + list + get.

class DatasetSpec(BaseModel):
    """A registered, versioned dataset (FR1, §13). Registration content-addresses the data: the bytes
    are snapshotted to immutable object storage keyed by their hash, and `content_hash`/`snapshot_uri`
    are pinned on the entity — so a dataset version is reproducible by content, not by a mutable path."""
    id: str
    version: int = 1
    source: str = "jsonl"                      # jsonl | hf | s3 | db …
    uri: str                                   # path/URI the registrar reads to snapshot
    description: str = ""
    content_hash: str | None = None            # server-populated at registration (sha256 of the bytes)
    snapshot_uri: str | None = None            # server-populated: immutable content-addressed copy


class EvalSpec(BaseModel):
    """A versioned eval bundle = dataset + default harness + default scorer(s) + config (FR2)."""
    id: str
    version: int = 1
    dataset: str                               # dataset id (the registered DatasetSpec.id)
    default_harness: PluginRef
    default_scorers: list[PluginRef]
    description: str = ""


class LaunchFromEval(BaseModel):
    """Launch a run *from* a registered eval (FR2/FR10): the eval supplies the dataset (its pinned
    content-addressed snapshot) + default harness/scorers; the caller chooses the model + run knobs."""
    model: str = "mockllm/model"
    batch_size: int = 50
    limit: int | None = None
    epochs: int = 1
    budget_usd: float | None = None
    mock_output: str | None = None


class ModelSpec(BaseModel):
    """A registered target/model (FR3): a logical name → provider + model id + default params."""
    id: str                                    # logical name, e.g. "gpt-4o-mini-prod"
    version: int = 1
    provider: str                              # openai | openrouter | vllm | mockllm
    model_id: str                              # the provider's model id
    params: dict = Field(default_factory=dict)  # default temperature/seed/… for this target
    description: str = ""


class RunSpec(BaseModel):
    eval: str
    eval_version: int = 1          # pin eval@version (DESIGN §7 — the eval is a versioned bundle)
    dataset: str
    model: str = "mockllm/model"
    harness: PluginRef
    scorers: list[PluginRef]
    team: str | None = None        # ownership (DESIGN §7) — tenancy enforcement is deferred (FUTURE.md §9)
    lane: str | None = None        # admission lane override: "interactive" | "batch"; else auto-classified (SCHEDULER §2)
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
    # Transcript retention (DESIGN §8/§13). Fraction of *passing* samples whose transcript is kept;
    # failing samples are always kept (stratified — failures are what you debug). None ⇒ fall back to
    # the EVAL_ENGINE_TRANSCRIPT_SAMPLE_RATE env (unset ⇒ keep all). Lets large runs sample-by-default
    # (cap storage) while small/interactive runs keep everything. Stored transcripts are zstd-compressed.
    transcript_sample_rate: float | None = None
    # Prototype-only convenience: fixed output for the mock model so runs are deterministic
    # and need no API keys. Ignored for real models.
    mock_output: str | None = None
    # Prototype-only convenience: a scripted tool-call sequence ([{tool, args}, ...]) for a mock
    # AGENTIC run — deterministic agent trajectory with no provider key. Ignored for real models.
    mock_tool_calls: list[dict] | None = None
