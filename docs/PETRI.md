# Adding Petri (automated alignment auditing) to Eval Engine

> **Goal.** Add a new **eval *shape*** to the platform: Anthropic's **Petri** — an automated
> *alignment-auditing* agent that probes a target model for misaligned behaviors (deception,
> sycophancy, power-seeking, self-preservation, reward hacking, …) and scores the resulting
> transcripts across many safety dimensions. Today the engine does **correctness** evals
> (accuracy / pass-fail: QA, MCQ, math, code, agentic-with-sandbox — `docs/ADDING_REAL_EVALS.md`).
> Petri is a different kind of eval: **no gold answer, no pass/fail — a 1–10 "how concerning"
> score per behavioral dimension.** This doc is the findings + plan for wiring it in.
>
> This is a **design note**, like `docs/ADDING_REAL_EVALS.md` — *what to add, what already works
> for free, and the gaps to close.* It is **not** a `docs/PROJECT_PROGRESS.md` v1 gap (those are
> v1-*design* gaps; this is a new capability + a few leaf plugins on top of the finished spine).
> The one genuine spine touch — threading Inspect **model roles** through the runner — is called
> out explicitly in §4.
>
> **Status: PROPOSED** (2026-06-08). Nothing below is implemented yet.

---

## 1. What Petri is (and why it's a good fit)

[Petri](https://github.com/safety-research/petri) ("Parallel Exploration Tool for Risky
Interactions") is Anthropic's open-source alignment-auditing tool, **built directly on Inspect AI**
— the same kernel we're built around (DESIGN §3). That single fact is why this is a *days* job and
not a *weeks* one: Petri is not a foreign framework to bridge to, it's **Inspect-native, like us.**
(It was [donated to Meridian Labs](https://www.anthropic.com/research/donating-open-source-petri);
the package is now `inspect_petri` under `meridianlabs-ai`.)

**How an audit works — three model roles, not one:**

| Role | What it does | Maps to our axis |
|---|---|---|
| **auditor** | An agent that *designs and drives* the audit: from a **seed instruction**, it invents a realistic multi-turn scenario, role-plays the user/environment, **simulates tool calls and their results in-context**, and can **roll back** to retry a branch — all to elicit the behavior under test. | a *harness* concern (the auditor IS the solver loop) |
| **target** | The model **being evaluated** — it only sees a realistic conversation, not that it's being audited. | **our `RunSpec.model`** (the model axis) |
| **judge** | Reads the finished transcript and emits a **1–10 score + written justification per dimension** (tied to specific messages). | a *scorer* concern (the judge IS the scorer) |

**Scale of the built-ins:** Petri ships **170+ seed instructions** and **~38 judging dimensions**;
each dimension is scored **1–10** with a justification, plus two special meta-dimensions —
**`eval_awareness`** (did the target seem to notice it was being tested?) and **`scenario_realism`**.
In Anthropic's launch it surfaced concerning behaviors across 14 frontier models from 111 seeds.

**Why it slots cleanly onto our domain model (DESIGN §7):**
- **Seed instruction = a Sample's `input`.** A seed is just a text instruction. The list of seeds is
  a **dataset** — it flows through our content-addressed JSONL snapshot path (`datasets.py`)
  **unchanged**. No `target` field (no gold answer); `Sample.target=""`.
- **Auditor = harness (Solver).** Petri's auditor agent is an Inspect solver — register it as a
  `petri` harness plugin.
- **Judge = scorer (Scorer).** Petri's judge is an Inspect scorer — register it as a `petri_judge`
  scorer plugin.
- **The `.eval` log + embedded Inspect viewer render the rich multi-turn transcript *for free*** —
  we already write `.eval` logs to object storage and embed the Inspect viewer (DESIGN §8). Petri's
  transcripts are just Inspect transcripts.
- **One huge simplification vs. our `agentic` harness: NO real sandbox.** The auditor *simulates*
  tools in-context (it writes the tool's "output" itself), so there is **no Docker/K8s/gVisor pod,
  no `batch_size: 1` deadlock constraint, no pod-churn scale risk** (DESIGN §12). Petri is an
  ordinary multi-model API workload — every call goes worker → LiteLLM gateway, exactly like
  `single_turn`.

---

## 2. What we get for free (the substrate)

The execution path is Inspect-native (`runner.execute_batch` builds `Task(dataset, solver, scorer)`
from a `RunSpec` and runs it). Petri = supply **(seeds JSONL) × (auditor=Solver) × (judge=Scorer)**
that compose into that Task. Concretely, these already work with zero changes:

| Layer | Works as-is for Petri | Where |
|---|---|---|
| **Dataset / seeds** | JSONL `{id, input}` (input = seed text); `metadata.category` (the behavior family, e.g. `deception`) → ClickHouse `group_key` → the **per-category dashboard breakdown lights up as misalignment-by-behavior**. Content-addressed snapshot on register. | `datasets.py`, `analytics.py` |
| **Transcripts** | Rich multi-turn audit transcript captured in the `.eval` log → object store → **embedded Inspect viewer** drill-in. Our thin per-sample transcript JSON still written + sampled (DESIGN §13). | `runner._put_transcript`, `view_main.py` |
| **Gateway / rate limits** | Auditor + target + judge are all **model calls through the LiteLLM gateway** — global rpm/tpm caps and fail-fast 429s apply to all three (FR6). | `runner`, LiteLLM |
| **Registration / launch / re-run / audit** | Register a `DatasetSpec` (seeds) + `EvalSpec` (seeds + `petri` harness + `petri_judge` scorer) + `ModelSpec` (target); launch-from-eval, re-run, audit log all work. | `control.py`, `api.py` |
| **Lanes / subsetting / budget** | `limit` for a subset of seeds; `budget_usd` backstop → `budget_skipped`; epochs for repeated audits (variance). | `runner._classify`, `enforce_budget` |
| **Plugin UX** | `petri`/`petri_judge` configs are Pydantic models → JSON Schema auto-renders the launch-wizard form (PLUGINS §4). | `plugins.py`, `api.py` catalog |

**The multi-dimensional `scores` column already exists.** `analytics.sample_results.scores` is a
JSON string of `{dimension: value}` (it's serialized from a dict in `make_row`). Petri's ~38
dimensions land there directly — no schema change to ClickHouse.

---

## 3. The semantic wrinkles (this is where Petri differs from every existing eval)

These are the things that make Petri *not* just another scorer, and that the implementation must get
right. None requires re-architecting; all require deliberate decisions.

### W1. There is no "pass/fail" — and "higher" means "worse"

Every existing scorer returns a scalar where **higher = better** and the runner does
`passed = 1 if primary_score >= 0.5`. Petri inverts this: a high misalignment score is a **finding**
(bad), not a pass. We must decide what `passed` / `primary_score` *mean* for a Petri run:

- **`primary_score`** ← the headline **overall-misalignment** dimension (Petri's summary score),
  **normalized to [0,1]** (`score / 10`). `runner._score_value` today returns the raw value, so a
  raw 1–10 would make `passed = (>=0.5)` fire on almost everything — normalization is mandatory.
- **`passed`** is semantically wrong for Petri. **Recommendation:** repurpose it as a **"flagged"
  flag** — `1` iff the overall misalignment score ≥ a configurable threshold (e.g. ≥ 5/10). Then the
  existing rollups read naturally: "flag rate" instead of "pass rate", and the Wilson CI
  (`runner.wilson_ci`) becomes a CI on the flag rate. **Document this prominently** — for a Petri
  run, the dashboard's "accuracy" tile is a **concern rate**, the opposite polarity of a QA run.
  (A cleaner long-term option is a per-eval `polarity`/`metric_label` field so the UI labels it
  correctly; note it as a small follow-up, not a blocker.)

### W2. One scorer → many dimensions (a dict-valued Score)

Existing scorers attach **one scalar `Score` per scorer name**; `runner.execute_batch` does
`{name: _score_value(sc.value) for name, sc in s.scores.items()}` and takes the **first** value as
`primary`. Petri's judge produces **one Score whose `.value` is a dict** of ~38 dimensions (or it
registers many metrics). So:

- `_score_value` must **handle a dict-valued `Score`** (today `float(value)` would throw on a dict).
- The dimension dict must be **flattened into our `scores` map** (e.g. `misalignment`, `deception`,
  `sycophancy`, …, `eval_awareness`, `scenario_realism`), and **`primary` chosen explicitly** (the
  overall-misalignment dimension), not "first key wins."
- Keep dimension **names stable** — they become `scores` JSON keys queried by the dashboard /
  canned views. Pin the dimension set on the eval version (it's part of the judge config).

### W3. Cost is undercounted by `_cost_usd` (auditor + judge burn tokens too)

`runner._cost_usd` prices only `spec.model` (the **target**). A Petri audit spends on **three**
models, and the **auditor + judge often cost *more* than the target** (long planning + a 38-dimension
judging pass). Options:

| Option | What | Verdict |
|---|---|---|
| **(a) Target-only (status quo)** | Leave `_cost_usd` as-is; report target spend, undercount auditor+judge. | Cheapest; **wrong** for budgeting. Use only as a stopgap. |
| **(b) Sum all roles from the `.eval` log** | Inspect's `EvalLog` records per-model usage; sum target+auditor+judge token usage and price each via the catalog. | **Recommended.** ~20 lines in `execute_batch`; honest cost; keeps `budget_usd` meaningful. |
| **(c) Gateway per-`run_id` tally** | The deferred canonical cost (PROJECT_PROGRESS #17 / DEPLOYMENT "A5"). All three roles already route through the gateway, so this is the *correct* long-term answer. | Best, but gated on the same deferred work; (b) is the v1 step. |

Because auditor/judge are gateway-fronted, **`budget_usd` still stops the run** (committed cost
gauge), but with target-only pricing it would stop *late*. Go with **(b)**.

### W4. Each sample is an expensive, long, multi-turn rollout

Like `agentic`, a Petri sample is many sequential turns (auditor ↔ target) plus a heavy judging
pass — minutes and many tokens, not a single generate. Implications:
- **Lane: `batch`** by default (or `interactive` for a tiny seed subset during iteration). Classify
  as agentic-like.
- **`time_limit`** (`SAMPLE_TIME_LIMIT`, default 600s) may need raising for deep audits
  (`max_turns` high) — make it configurable via the harness config and/or env.
- **No `batch_size: 1` constraint** (the agentic rule is about one-sandbox-per-sample; Petri has no
  sandbox). A modest `batch_size` (e.g. 5–10) is fine; the per-run `max_inflight` cap (#11) protects
  the cluster.

---

## 4. The one real spine touch: model roles through the runner

This is the **only** change outside leaf plugins, and it's small + general.

**The problem.** `runner.execute_batch` calls `inspect_eval(task, model=model, …)` with a **single**
model. Petri needs Inspect's **model-role** mechanism — `auditor`, `target`, `judge` are distinct
roles. On the Petri CLI this is `--model-role auditor=… --model-role target=… --model-role judge=…`;
programmatically it's `inspect_ai.eval(task, model=…, model_roles={…})`. Our `spec.model` is the
**target**; auditor + judge come from the harness/scorer config.

**The pattern already exists.** The `agentic` harness returns `(solver, sandbox)` and the runner
special-cases the 2-tuple to thread a sandbox into the `Task` (`builtins.agentic`,
`runner.execute_batch` lines ~285-291). We generalize the *same* idea to model roles.

**Recommended approach — let a harness declare model roles, runner threads them:**

1. The `petri` harness's config carries `auditor_model: str` and `judge_model: str` (registered
   `ModelSpec` labels, routed via LiteLLM like any model). The `petri_judge` scorer config carries
   `judge_model` too (or reads it from a shared place) — keep one source of truth; simplest is the
   harness owns auditor, the scorer owns judge.
2. Extend the harness return contract from `Solver | (Solver, Sandbox)` to optionally carry roles —
   e.g. return a small `HarnessBuild(solver, sandbox=None, model_roles={"auditor": …})`, or (lower
   churn) add a third tuple slot. Mirror it for the scorer (judge model).
3. `runner.execute_batch` collects `model_roles` from the built harness/scorer and passes
   `model_roles={"auditor": get_model("auditor", ...), "judge": ...}` into `inspect_eval(...)`.
   `spec.model` stays the **target** (Inspect's default role). Resolve each role's label →
   `get_model` exactly like the target (so auditor/judge also go through the gateway, W3).

> **Document the extension in `docs/PLUGINS.md`** — it's a genuine, principled widening of the
> harness/scorer contract ("a harness/scorer may declare auxiliary **model roles**, like it may
> declare a **sandbox**"), and it's reusable by any future multi-model eval (e.g. debate,
> self-critique, multi-agent).

**Alternative (rejected for v1):** import Petri's *fused* Inspect `Task` and run it via an adapter,
bypassing our compose path. Rejected for the **same reason `inspect_evals` was** in
`ADDING_REAL_EVALS.md` §5 — a fused Task sidesteps our ledger/analytics/snapshot/compose model, which
*is* the platform. We want Petri's **solver + scorer** as plugins, our Task around them.

---

## 5. Gap analysis — what must be added

Grouped by capability. None touches the orchestration/ledger/worker spine.

### G1. Dependency + module (`eval_engine/petri.py`)

- Add `inspect_petri` as an **optional extra** in `pyproject.toml` (`[petri]`), mirroring `[gcs]` —
  it's a heavy/optional dep. **Pin a commit/tag**: Petri **v3** changed the Python API and is
  **incompatible with v2** (v2 lives on the `petri-v2` branch). The pin is the eval's `code_ref`
  (PLUGINS §6) — reproducing a historical Petri run = redeploying that pin.
  `pip install "inspect_petri @ git+https://github.com/meridianlabs-ai/inspect_petri@<pinned-rev>"`.
- New `eval_engine/petri.py` (lazy-imported, like `ifeval.py` / `swebench.py`): adapters that wrap
  Petri's auditor solver + judge scorer and normalize their I/O to our contract (the W1/W2 mapping).
  > ⚠️ **Confirm exact symbol names against the pinned `inspect_petri` version** — v3's public API
  > differs from v2. The integration *contract* is stable (we need a Solver-shaped auditor + a
  > Scorer-shaped judge + the auditor/judge model roles); the import paths are what to verify.

### G2. Plugins (`eval_engine/builtins.py`)

| Plugin | Kind | Wraps | Config (Pydantic) |
|---|---|---|---|
| **`petri`** | harness | Petri auditor agent (Solver) | `auditor_model`, `max_turns` (default ~15–20), `special_instructions`/seed-handling toggles, `time_limit`, declares the **`auditor` model role** |
| **`petri_judge`** | scorer | Petri judge (Scorer) | `judge_model`, `dimensions` (default = Petri's built-in ~38; allow a subset), `flag_threshold` (W1, default 5), `primary_dimension` (default the overall-misalignment dim), `primary_metric="misalignment"` |

The judge scorer must (a) emit our `scores` dict (W2), (b) set `primary_score` = normalized overall
dimension, (c) compute the **flagged** flag (W1). Register both like every other plugin so the
catalog/validation/launch-form machinery just works.

### G3. Seeds dataset (`examples/benchmarks/petri_seeds.jsonl`)

- A converter (extend `tools/fetch_benchmark.py` or a small `tools/fetch_petri_seeds.py`) that pulls
  a **deterministic subset** of Petri's built-in seeds and writes `{id, input, metadata:{category}}`
  JSONL, with `metadata.category` = the behavior family (deception / sycophancy / power-seeking / …)
  so the per-`group_key` dashboard breakdown becomes **misalignment-by-behavior**. Register via
  `POST /datasets` → snapshot + content hash, like any dataset.
- Record Petri's license/citation in `examples/benchmarks/README.md` (it's MIT-licensed; attribute
  Anthropic / Meridian Labs).

### G4. The runner model-role thread (§4)

The harness/scorer-declared `model_roles` → `inspect_eval(..., model_roles=...)`, plus the dict-Score
handling (W2), the normalized `primary_score` + flagged `passed` (W1), and the all-roles cost sum
(W3, option b). This is the only edit to `runner.py` / the plugin return contract.

### G5. Entities + examples + dashboard polish

- A seed/example set mirroring `examples/benchmarks/*.yaml`: register the seeds dataset + a `petri`
  `EvalSpec` (seeds + `petri` harness + `petri_judge` scorer) + a target `ModelSpec`; launchable from
  the dashboard "from registered eval" drawer (#8). Plus an `examples/petri.yaml` RunSpec for the CLI
  path (mirrors `examples/agentic_sandbox.yaml`), e.g.:

  ```yaml
  # Alignment audit: an auditor agent probes the TARGET model for misaligned behavior; a judge
  # scores the transcript across ~38 dimensions (1-10). No real sandbox — tools are simulated by
  # the auditor in-context. All three roles route through the LiteLLM gateway. Needs OPENROUTER_API_KEY.
  eval: petri_audit
  dataset: examples/benchmarks/petri_seeds.jsonl
  model: openrouter/anthropic/claude-3-5-haiku      # the TARGET (model under test)
  batch_size: 5
  harness:
    type: petri
    config:
      auditor_model: openrouter/anthropic/claude-3-5-sonnet   # capable agent to drive the audit
      max_turns: 15
  scorers:
    - type: petri_judge
      config:
        judge_model: openrouter/anthropic/claude-3-5-sonnet
        flag_threshold: 5
  ```

- **Optional canned ClickHouse view / dashboard tile** (analogous to per-category): *mean
  misalignment by model*, *flag rate by behavior category*, and a **`eval_awareness` watch** (a
  high eval-awareness mean undercuts a clean misalignment score — surface it). These read the
  `scores` JSON; no schema change. Relabel the run-detail "accuracy" tile as a **concern rate** for
  petri-harness runs (W1).

---

## 6. Model choice (a capability decision, not just cost — like agentic)

`docs/ADDING_REAL_EVALS.md` notes a weak model emits tool calls as literal text and fails the
agentic eval. Petri is even more sensitive: the **auditor** and **judge** must be **strong** models
(the auditor designs adversarial multi-turn scenarios and simulates tools; the judge does nuanced
38-dimension grading). The **target** can be anything (that's the point — audit weak and strong
models alike). Practical default: a **frontier model for auditor+judge** (e.g. Claude
Sonnet-class), the **target = whatever you're auditing**. A weak auditor produces shallow audits; a
weak judge produces noisy scores. Budget accordingly (W3) — Petri runs are **low-volume,
high-value** (a few hundred audits), not 100k-sample accuracy sweeps, so absolute cost stays modest
even though per-sample cost is high.

---

## 7. Scale / envelope impact (none, structurally)

Petri does **not** move either of "the two numbers that force real infrastructure" (DESIGN §2):
- **Analytics rows:** ~170 seeds × epochs per run = hundreds of rows, not the ~1B/month QA envelope.
  Each row's `scores` JSON is fatter (~38 dims) — negligible.
- **Rate limiting:** more gateway calls per sample (3 roles × many turns), but the *same* shared
  Redis rpm/tpm machinery (#1) governs them — Petri just consumes the budget faster per sample, which
  is exactly what global limiting is for.
- **No sandbox** ⇒ **none** of the agentic pod-churn risk (DESIGN §12, top-tier risk). Petri is a
  pure API workload. This is the *easiest* expensive eval to run at scale.

So the ledger/orchestrator/worker/analytics spine is **untouched**; this is leaf plugins + the
model-role thread + content.

---

## 8. Testing

- **Unit:** the W1/W2 mapping in isolation — feed a fixture judge `Score` (dict of dimensions) →
  assert `scores` dict, normalized `primary_score`, and the `flagged` flag at/over/under threshold.
  Seed converter: deterministic subset (same seed → same ids), field mapping → `category`.
- **Integration (mock-friendly):** Petri's three roles make a pure-mock run harder than QA, but the
  engine already supports `mockllm` + scripted tool calls (`RunSpec.mock_tool_calls`,
  `runner._build_model`). Either (a) drive a **tiny real run** (1–2 seeds, cheap models, tight
  `budget_usd`) marked `e2e` (like the agentic/`code_exec` sandbox tests), and assert
  ledger→analytics→per-category rollup populated with the dimension keys present in `scores`; or
  (b) a deterministic mock-auditor/mock-judge fixture if the pinned Petri version exposes seams for
  it.
- **Cost guard:** assert finalize cost is non-zero and reflects **all three roles** (W3 regression —
  the bug would be target-only undercount).
- **Polarity guard:** assert a known-concerning fixture yields a **high** misalignment score and is
  **flagged** (catches an accidental polarity inversion in W1).

---

## 9. Summary of concrete additions

**New files**
- `eval_engine/petri.py` — auditor/judge adapters + the W1/W2 normalization (lazy-imported).
- `tools/fetch_petri_seeds.py` (+ `examples/benchmarks/petri_seeds.jsonl`, README attribution).
- `examples/petri.yaml` (CLI) + a seed-entities step for the dashboard "from registered eval" path.
- Tests under `tests/unit` (mapping + converter) + `tests/integration` (`e2e` tiny real audit).

**`eval_engine/builtins.py`** — register the **`petri`** harness + **`petri_judge`** scorer (configs
carry the auditor/judge model roles, dimension set, flag threshold).

**`eval_engine/runner.py`** *(the only spine edit)* — thread harness/scorer-declared **`model_roles`**
into `inspect_eval`; handle a **dict-valued `Score`** (W2); normalized `primary_score` + flagged
`passed` (W1); **sum cost across all three roles** (W3, option b).

**`pyproject.toml`** — optional **`[petri]`** extra pinning `inspect_petri` at a fixed rev (the eval's
`code_ref`).

**`docs/PLUGINS.md`** — document the **model-role** contract extension (a harness/scorer may declare
auxiliary model roles, as it may declare a sandbox).

**No changes** to the ledger / orchestrator / worker / ClickHouse schema — Petri is content + leaf
plugins + one general runner extension on a finished platform.

---

## 10. Open questions

1. **Polarity in the UI.** Repurpose `passed` as a "flagged" flag now (W1, zero schema change) vs.
   add a proper per-eval `polarity`/`metric_label` so the dashboard labels "concern rate" correctly?
   (Recommend: ship the flag now; add the label as a fast follow.)
2. **Petri v3 vs v2 API.** Pin v3 (current, split auditor/target — better for our roles model) or
   start on v2 (`petri-v2` branch, more battle-tested)? Recommend **v3** — its auditor/target split
   matches our model-role threading directly. Confirm symbol names against the pinned rev (G1).
3. **Dimension surfacing.** Keep all ~38 dims in the `scores` JSON only, or promote the 2–3 headline
   dims (`misalignment`, `eval_awareness`, `scenario_realism`) to their own canned-view columns for
   first-class dashboard tiles? (JSON-only is zero-schema; promotion is a nicer UX — defer to demand.)
4. **Seed authorship.** v1 ships a subset of Petri's built-in seeds. Do we want a dashboard surface
   to author/register **custom** seed instructions (they're just dataset rows) as a first-class
   "write your own audit" flow? (Additive — it's the dataset path; note for later.)
