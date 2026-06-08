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
    temperature: float | None = None
    seed: int | None = None
    transcript_sample_rate: float | None = None   # None ⇒ env default; 1.0 ⇒ keep all transcripts


class ModelSpec(BaseModel):
    """A registered target/model (FR3): a logical name → provider + model id + default params."""
    id: str                                    # logical name, e.g. "gpt-4o-mini-prod"
    version: int = 1
    provider: str                              # openai | openrouter | vllm | mockllm
    model_id: str                              # the provider's model id
    params: dict = Field(default_factory=dict)  # default temperature/seed/… for this target
    description: str = ""


# --- Training monitor (docs/TRAINING_MONITOR.md). A new vNext track: we MONITOR an external (mocked)
# trainer's checkpoint stream and continuously eval each checkpoint. These are additive — an ordinary
# run never touches them.

class SuiteEntry(BaseModel):
    """One eval in a training run's suite (run on EVERY checkpoint). ``role='canary'`` marks a trivial
    sanity eval any functional model should ace — a canary collapse is a decisive training/serving-fault
    signal that separates a broken checkpoint from a model merely weak on a hard eval (§8)."""
    eval: str                                   # registered EvalSpec id
    version: int = 1
    role: str = "standard"                      # standard | canary
    color: str | None = None                    # chart line color (display only)
    sample_limit: int | None = None             # per-eval dataset subsample override (cost vs cadence)


class TrainingRunConfig(BaseModel):
    """Per-training-run cost/cadence policy — fully configurable per run. Lets a cheap run sub-sample
    every checkpoint while a milestone run evals the full suite at full size (the user's ask)."""
    eval_every: int = 1                         # eval every Kth discovered checkpoint
    sample_limit: int | None = None             # default per-checkpoint dataset subsample (None = full)
    milestone_every: int | None = None          # every Nth evaluated checkpoint runs the FULL suite full-size
    max_pending_checkpoints: int | None = None  # skip-stale safety valve: cap the un-evaluated backlog (keep latest)
    lane: str = "interactive"                   # admission lane for checkpoint-evals (timely feedback)
    epochs: int = 1
    budget_usd: float | None = None             # optional aggregate budget across the whole training run


class TrainingRunSpec(BaseModel):
    """A training run we MONITOR (the trainer is mocked / out of scope, DESIGN §1 non-goal). Registered
    then watched: the monitor polls ``source`` for new checkpoints and evals each against ``suite``."""
    id: str
    model: str                                  # the model being trained (display + ckpt model-id prefix)
    base: str = ""                              # base/seed model
    planned_steps: int = 0
    source: str                                 # object-storage prefix we poll (gs://… or a local dir)
    suite: list[SuiteEntry]
    owner: str = ""
    hardware: str = ""                          # provenance/display, e.g. "256× H100"
    precision: str = ""                         # provenance/display, e.g. "bf16"
    glyph: str = ""                             # 2-char avatar (display)
    started: str | None = None
    config: TrainingRunConfig = Field(default_factory=TrainingRunConfig)


class CheckpointManifest(BaseModel):
    """The per-checkpoint contract the (mocked) trainer writes — nothing about *how* it was trained.
    ``model_ref`` is the opaque handle the engine evaluates *as if* trainer infra served it; a mock
    resolver maps it to a real model behind the scenes (§4)."""
    training_run_id: str
    step: int
    model_ref: str
    tokens: int = 0
    wall_time: str | None = None
    train_metrics: dict = Field(default_factory=dict)   # {loss, grad, lr, throughput} — §8 cross-check


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
    # gateway's own per-run_id reject (the deferred canonical cost tally); this enforces the same
    # semantics control-side.
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
