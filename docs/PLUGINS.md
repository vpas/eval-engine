# Eval Engine — Extensibility / Plugin Interface (Draft v0.1)

> Companion to `DESIGN.md` v0.2. Defines the contract for adding **harnesses, scorers,
> dataset loaders, and tools** without touching the core — the extensibility guarantee.
> Built on **Pure A** (D3): harnesses *are* Inspect `Solver`s, scorers *are* Inspect
> `Scorer`s. The plugin layer is a thin, typed, discoverable wrapper around Inspect's own
> registry — not a re-abstraction of it.

---

## 1. Goals & principles

- **Add a harness/scorer/loader/tool as a self-contained Python unit**; the core discovers
  it, the dashboard renders its config, the workers instantiate it — no core edits.
- **Typed config with a published schema.** Each plugin declares a Pydantic config model →
  auto-derived JSON Schema drives (a) RunSpec validation and (b) the launch-wizard form.
- **Versioned & reproducible.** A plugin declares a semantic `version`; the *actual code* is
  pinned by the deployment's `code_ref` (git sha / package version) recorded on the eval
  version. Reproducing a run = redeploying that `code_ref`.
- **Discoverable without executing.** Plugins register via Python **entry points**; a sync
  step imports their *metadata* and upserts a **catalog in Postgres**, so the control plane
  serves the catalog to the dashboard without importing plugin code at request time.
- **Stay idiomatic to Inspect.** A harness returns an Inspect `Solver`; a scorer returns an
  Inspect `Scorer`. We don't wrap those types — we wrap *registration + config + metadata*.

---

## 2. The four plugin kinds

| Kind | Wraps (Inspect) | Returns | Purpose |
|---|---|---|---|
| **harness** | `@solver` | `Solver` | How the model is driven (single-turn, agentic, RAG, …). |
| **scorer** | `@scorer` | `Scorer` | How output is judged (programmatic, LLM-judge, match, human-stub). |
| **dataset_loader** | — | `Dataset` (samples) | Turn a source (HF/S3/JSONL/DB) into a content-addressed snapshot. |
| **tool** | `@tool` | `Tool` | Capabilities an agentic harness can call (search, code-exec, http…). |

---

## 3. Authoring contract

### 3.1 Harness

```python
from eval_engine.plugins import harness
from inspect_ai.solver import Solver, basic_agent
from pydantic import BaseModel, Field

class SandboxSpec(BaseModel):                                # see docs/SANDBOXING.md §4–§6
    tier: str = Field("T2", pattern="^T[123]$",             # T1 benign / T2 gVisor / T3 microVM
                      description="isolation tier; default T2 (gVisor, air-gapped)")
    egress_allowlist: list[str] = Field(default_factory=list,
                      description="FQDNs the tool egress proxy may reach (T1 only)")
    cpu: str = "1"; memory: str = "2Gi"; pids: int = 256    # resource caps (baseline hardening)

class AgenticConfig(BaseModel):
    tools: list[str] = Field(default_factory=list, description="tool plugin names")
    max_steps: int = Field(20, ge=1, le=200)
    sandbox: SandboxSpec = Field(default_factory=SandboxSpec)

@harness(
    name="agentic",
    version="1.2.0",
    config=AgenticConfig,
    description="Tool-using agent loop with tiered K8s-sandboxed execution.",
)
def agentic_harness(cfg: AgenticConfig) -> Solver:          # factory → Inspect Solver
    # The orchestrator provisions the tier's ephemeral sandbox pod (SANDBOXING §7) and wires
    # Inspect's k8s sandbox provider; the harness just declares requirements + builds the loop.
    return basic_agent(
        tools=[resolve_tool(t) for t in cfg.tools],
        max_attempts=cfg.max_steps,
        sandbox=("k8s", cfg.sandbox.model_dump()),
    )
```

### 3.2 Scorer

```python
from eval_engine.plugins import scorer
from inspect_ai.scorer import Scorer, model_graded_qa
from pydantic import BaseModel

class JudgeConfig(BaseModel):
    judge_model: str               # a registered Target label, routed via LiteLLM
    rubric: str                    # grading template
    partial_credit: bool = False

@scorer(
    name="llm_judge",
    version="2.0.0",
    config=JudgeConfig,
    primary_metric="score",        # which output is the run's primary_score (SCHEMA §2)
    description="Model-graded scoring against a rubric.",
)
def llm_judge(cfg: JudgeConfig) -> Scorer:
    return model_graded_qa(model=cfg.judge_model, template=cfg.rubric,
                           partial_credit=cfg.partial_credit)
```

Custom **programmatic** scorers are just a scorer plugin returning an Inspect `Scorer`
built from a plain Python function — arbitrary scoring code, same contract. The `human`
scorer ships as a registered stub (`primary_metric="human_score"`) so its outputs slot into
the same schema; its UI is deferred (D10).

### 3.3 Dataset loader & tool (sketch)

```python
@dataset_loader(name="huggingface", version="1.0.0", config=HFConfig)
def load_hf(cfg: HFConfig) -> Dataset: ...     # → normalized samples → snapshotted (§0 SCHEMA)

@tool(name="web_search", version="1.0.0", config=SearchConfig)
def web_search(cfg: SearchConfig) -> Tool: ...  # referenced by harness configs
```

---

## 4. Registration, discovery & catalog sync

### 4.1 Entry points (packaging)

A plugin package declares entry points; first-party plugins live in `eval_engine.builtins`,
but any pip-installed package can contribute (supports per-team / third-party plugins):

```toml
# pyproject.toml of a plugin package
[project.entry-points."eval_engine.plugins"]
my_team_harnesses = "my_team_evals.harnesses"
my_team_scorers   = "my_team_evals.scorers"
```

At import, the `@harness/@scorer/...` decorators populate an **in-process registry**
keyed by `(kind, name, version)`.

### 4.2 Catalog sync → Postgres

A CLI/init step (run at image build & deploy) imports all entry-point modules and upserts
each plugin's *metadata* into the catalog. This decouples the **control plane** (reads the
catalog from Postgres; never imports plugin code) from the **workers** (import and execute
plugin code).

```sql
CREATE TABLE plugins (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind          text NOT NULL CHECK (kind IN ('harness','scorer','dataset_loader','tool')),
  name          text NOT NULL,
  version       text NOT NULL,                 -- declared semver
  config_schema jsonb NOT NULL,                -- JSON Schema derived from the Pydantic config
  primary_metric text,                         -- scorers only
  description   text,
  code_ref      text NOT NULL,                 -- package/git version this row was synced from
  registered_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (kind, name, version)
);
```

```
$ eval-engine plugins sync --code-ref $(git rev-parse HEAD)   # CI/deploy step
  discovered: harness/agentic@1.2.0, scorer/llm_judge@2.0.0, ...
  upserted 14 plugin versions into catalog (code_ref=ab12cd…)
```

---

## 5. How a run resolves plugins

A RunSpec references plugins by `{type, version, config}` (SCHEMA §1.5):

```jsonc
{
  "harness_config": { "type": "agentic", "version": "1.2.0",
                      "config": { "tools": ["web_search"], "max_steps": 30 } },
  "scorer_config":  [ { "type": "llm_judge", "version": "2.0.0",
                        "config": { "judge_model": "gpt-4o-2024-11", "rubric": "..." } } ]
}
```

- **Validation (control plane, launch time):** look up each `(kind, type, version)` in the
  catalog; validate `config` against its `config_schema`. Reject unknown/mismatched plugins
  or invalid config *before* the run is created. The eval's `config_schema` (eval_versions)
  is the composition of its harness+scorer schemas, so the launch wizard renders a form
  straight from JSON Schema.
- **Instantiation (worker, execution time):** resolve `(kind, type, version)` in the
  in-process registry → call the factory with the parsed config → get the Inspect
  `Solver`/`Scorer` → run via the Inspect `Task`. Multiple scorers compose as an Inspect
  scorer list; harnesses compose as Inspect solver chains.

---

## 6. Versioning & reproducibility

Two coordinates, both recorded on the RunSpec/eval version:
1. **Declared plugin version** (`agentic@1.2.0`) — human-meaningful, used for catalog lookup
   and config-schema selection.
2. **`code_ref`** (git sha / built package version) on `eval_versions` — the *authoritative*
   pin of the executing code. The declared version is a label; `code_ref` is the truth.

**Rule:** the worker image's synced catalog `code_ref` must match the run's `eval_version.code_ref`
(or be compatible) — enforced at admission. Reproducing a historical run means deploying a
worker image at that `code_ref`. This keeps "what code actually ran" unambiguous even if a
plugin's declared version was reused or mutated.

> Recommendation: treat declared `version` as **immutable once synced** — a behavior change
> requires a new version. Catalog `UNIQUE(kind,name,version)` enforces no silent overwrite
> across a *different* `code_ref` (sync fails on conflict with a differing schema → forces a
> version bump). This is the guardrail that makes reproducibility real.

---

## 7. Security / trust boundary

Harnesses, programmatic scorers, and tools are **arbitrary Python executing on workers** —
a real trust boundary:
- **v1 (trusted team, D6):** first-party + team-reviewed plugins only; plugins run in the
  worker process. Acceptable given a single trusted team.
- **Agentic tool execution** (model-generated code/commands) is the genuinely untrusted part
  and already runs in **Inspect's Docker sandbox** — that isolation is mandatory regardless.
- **Later (multi-tenant):** untrusted *plugin* code would need its own isolation (separate
  worker pools per trust level, or sandboxed plugin execution) + a plugin-approval workflow
  gating catalog sync. Schema is ready (`team_id` on entities); enforcement deferred with
  tenancy.

---

## 8. What this buys us

- Adding an eval capability = ship a plugin package + `plugins sync`; the dashboard, config
  validation, and execution pick it up with **zero core changes** — the modularity goal.
- The `Target ⟂ Harness ⟂ Scorer ⟂ Dataset` orthogonality (DESIGN §7) is enforced by these
  being four independent registries composed only at RunSpec resolution.
- Config-schema-driven UI means new plugins are immediately usable from the launch wizard
  without frontend work.

---

## 9. Open questions for v0.3
1. **Compatibility policy for `code_ref` vs declared version** — exact match required, or a
   compatibility range? (Exact is simplest/safest; ranges ease ops.)
2. **Tool ↔ harness coupling** — do tools need their own config-schema surfaced in the
   launch UI, or are they fixed per-harness-config?
3. **Built-in catalog seeding** — which first-party harnesses/scorers ship in v1 (likely:
   `single_turn`, `multiple_choice`, `agentic`; `match`, `llm_judge`, `programmatic`,
   `human`-stub)?
4. **Plugin test contract** — a required self-test (golden sample) each plugin must pass in
   CI before `plugins sync` accepts it?
```
