# Adding real evals / benchmarks to Eval Engine

> **Goal.** Seed the platform with a *representative* set of real, name-brand benchmarks — the kind
> Anthropic reports in Claude model cards — so the engine and dashboard demo on something other than
> `capitals_qa`. The set should span **different eval *shapes*** (knowledge MCQ, free-form math,
> code-execution, instruction-following, agentic) to exercise the harness/scorer/analytics/UX surface.
> **Compute budget: keep the whole suite < $10** by running deterministic **subsets** of each benchmark
> on a **cheap model**.
>
> This doc is the findings + plan: *what to add, what already works, and the gaps to close.* It is a
> design note, not a checklist item in `PROJECT_PROGRESS.md` (those are v1-design gaps; this is content
> + a few small plugins on top of a finished spine).

> **Status (2026-06-06): IMPLEMENTED.** All six benchmarks below are wired end-to-end and tested.
> - **Plugins** (`eval_engine/builtins.py`): scorers `math`, `numeric_answer`, `code_exec`, `ifeval`;
>   harness `code_generation`; `single_turn` gained `system` + `prompt_suffix`. IFEval verifiers live in
>   `eval_engine/ifeval.py` (22 instruction families).
> - **Data** (`tools/fetch_benchmark.py` → `examples/benchmarks/*.jsonl`): deterministic subsets of
>   GPQA Diamond (50), MMLU (60), GSM8K (50), MATH-500 (40), HumanEval (40), IFEval (50).
> - **Launch** (`examples/benchmarks/*.yaml` for the CLI; `tools/seed_benchmarks.py` for the dashboard).
> - **Tests:** 96 unit + 2 full-spine integration (mock model: ledger→analytics→category rollup) + a
>   `code_exec` sandbox e2e (correct→pass / wrong→fail). The full benchmark suite has **not** been run
>   against a real model yet (needs `OPENROUTER_API_KEY` + the running cluster) — see §6 for the budget.
> - **Known limitation:** the `math` scorer (Inspect core, sympy) handles integers/fractions/decimals/√
>   reliably but is finicky on bare algebraic (`x+1`) and complex (`6+9i`) answers — an inherent scorer
>   limitation shared by upstream MATH harnesses, not a wiring bug.
> - **SWE-bench Lite**: IMPLEMENTED (2026-06-06) — `swe_bench` harness (bash agent in each instance's
>   official `swebench/sweb.eval.x86_64.*` image at /testbed, per-sample sandbox) + `swe_bench` scorer
>   (runs the precomputed, embedded eval script; resolves via FAIL_TO_PASS/PASS_TO_PASS). The runtime
>   stays lean: `swebench` is **tooling-only** (the converter precomputes each instance's eval script),
>   and the PyTest log-parsers are vendored into `eval_engine/swebench.py`. Subset narrowed to the
>   PyTest-family Lite repos the grader covers. **multimodal**: still deferred (§2).

---

## 1. What the engine supports today (the substrate we build on)

The execution path is **Inspect-native**: `runner.execute_batch` builds an Inspect `Task(dataset, solver, scorer)`
from a `RunSpec` and runs it. So adding a benchmark = supplying **(dataset JSONL) × (harness=Solver) ×
(scorer=Scorer)** that compose into that Task. Concretely:

| Layer | Today | Where |
|---|---|---|
| **Dataset** | JSONL → Inspect `MemoryDataset`; record fields `{id, input, target, choices, metadata}`. `metadata.category` → ClickHouse `group_key` (the per-category dashboard breakdown). Content-addressed snapshot on register. | `datasets.py`, `analytics.py` |
| **Harnesses** | `single_turn` (generate), `multiple_choice` (lettered MC + optional CoT), `agentic` (tool-use in a sandbox). | `builtins.py` |
| **Scorers** | `includes`, `match`, `choice`, `llm_judge` (model-graded). | `builtins.py` |
| **Cost** | tokens × OpenRouter catalog price (gateway fronts OpenRouter at catalog rate). Per-run `budget_usd` cap → `budget_skipped` terminal class. | `runner._cost_usd` |
| **Subsetting** | `RunSpec.limit` (first-N) and per-eval `sample_limit`. Subset = interactive lane (cheap, fast feedback). | `runner._classify` |
| **Stats** | `epochs` (repeat N×), Wilson CI on pass-rate, per-category rollup, model-vs-model compare. | `runner`, `analytics` |
| **Entities** | register `DatasetSpec`/`EvalSpec`/`ModelSpec` (versioned, immutable); launch from a registered eval; re-run; audit. | `control.py`, `api.py` |

**Two facts that shrink the work a lot:**

1. **Inspect *core* (0.3.235, already a dep) ships more scorers than we registered.** Available but
   un-wrapped: `math` (mathematical-equivalence answer checking — sympy-style), `answer('letter'|'word'|'line')`
   (extracts `ANSWER: X`), `pattern(regex)`, `exact`, `f1`, `pass_at` (code pass@k reducer). We only need
   to *register* these as plugins — no reimplementation.
2. **`Sample` supports per-sample `sandbox`, `files`, `setup`, and `metadata`** — so a code benchmark can
   carry its unit-test code in `metadata` and a scorer can execute it. Nothing in the schema blocks this.

What is **not** installed: the separate **`inspect_evals`** package (the curated benchmark task library:
GPQA, MMLU, MATH, GSM8K, HumanEval, IFEval, SWE-bench, …). It's an option (see §5), but it pulls full
*Inspect Tasks* (dataset+solver+scorer fused), which sidesteps our compose model and our content-addressed
JSONL snapshot. Recommendation: **mine it for reference, but ingest data ourselves** to keep runs flowing
through our ledger/analytics/snapshot path (that *is* the thing we're demoing).

---

## 2. The representative benchmark set

Chosen to (a) appear in recent Claude model cards and (b) each demonstrate a **distinct eval shape**. Sizes
are deterministic subsets sized for the budget (§6).

| # | Benchmark | Shape it showcases | In Claude model cards | Full size | Suggested subset | Supported today? |
|---|---|---|---|---|---|---|
| 1 | **GPQA Diamond** | Hard MCQ (graduate science) | ✅ headline reasoning metric | 198 | **50** | ✅ `multiple_choice` + `choice` |
| 2 | **MMLU** (few subjects) | MCQ knowledge, **per-category UX** | ✅ | 14 042 | **~60** (3–4 subjects × ~16) | ✅ `multiple_choice` + `choice` |
| 3 | **GSM8K** | Free-form **numeric** math | ✅ | 1 319 | **50** | ◐ needs an answer-extraction scorer |
| 4 | **MATH-500** | Free-form **symbolic** math (`\boxed{}`) | ✅ (MATH) | 500 | **40** | ◐ needs the `math` scorer wrapped |
| 5 | **HumanEval** | **Code generation + test execution** in a sandbox | ✅ | 164 | **40** | ✗ needs a code-gen harness + sandbox `code_exec` scorer |
| 6 | **IFEval** | **Instruction-following**, programmatic verifiers | ✅ | 541 | **50** | ✗ needs an `ifeval` scorer |
| 7 | **SWE-bench Verified** | **Agentic** software-engineering (repo + patch + tests) | ✅ headline agentic metric | 500 | **2–3** *(demo only)* | ◐ agentic harness exists; heavy per-task infra |
| 8 *(optional)* | **LLM-judge open-ended** (e.g. a small writing/QA slice) | Model-graded rubric scoring | — | — | **20** | ✅ `single_turn` + `llm_judge` |

**Shape coverage:** MCQ (1,2) · numeric (3) · symbolic-math (4) · code-exec (5) · programmatic-constraint
(6) · agentic (7) · model-graded (8). That's the full spread the UX should be able to render.

**Notes on scope:**
- **MMLU is the UX star** — pick a few subjects (e.g. `high_school_mathematics`, `professional_law`,
  `college_biology`) and stuff the subject into `metadata.category`. The dashboard's per-`group_key`
  breakdown and model-vs-model-by-category compare then light up on real data.
- **SWE-bench Verified** is included for completeness but is **not** a budget-friendly suite member: each
  task needs a multi-GB task-specific Docker image, repo checkout, patch application, and the project's
  own test runner. Treat it as a **1–3 instance showcase** of the agentic + gVisor sandbox path (#18 is
  done), not a scored subset. Full integration is a project of its own — flag it, don't block on it.
- **Multimodal (MMMU, vision)** is **out of scope**: our JSONL `input` is text and the loader has no
  image-content path. Note as deferred (akin to a FUTURE.md item) rather than a gap to fill now.

---

## 3. Gap analysis — what must be added

Grouped by capability. Each gap is small; none touches the orchestration/ledger spine.

### G1. Dataset ingestion: HF → our JSONL (the biggest *content* task)

`DatasetSpec.source` advertises `hf | s3 | db`, but `datasets.py` only reads `jsonl`/local/`gs://`. We need
to get benchmark data into our `{id, input, target, choices, metadata}` schema **and** keep it
content-addressed/snapshotted.

**Recommendation: a one-shot converter script, not a live `hf` loader.** Add `tools/fetch_benchmark.py`
(or `eval_engine/benchmarks/`) that, per benchmark, uses Inspect's already-present `hf_dataset` (or
`datasets.load_dataset`) to pull the rows, **deterministically subsamples** (fixed seed / stable hash so
the subset is reproducible and re-runs hit the same items), maps fields, and writes a JSONL into
`examples/benchmarks/<name>.jsonl`. Then register it via `POST /datasets` so it gets snapshotted +
content-hashed exactly like any other dataset. Rationale: offline-reproducible, pinned by content, and the
demo data lives in-repo (small subsets) — no network at run time, no surprise HF schema drift mid-demo.

Per-benchmark field mapping (the converter's job):

| Benchmark | `input` | `target` | `choices` | key `metadata` |
|---|---|---|---|---|
| GPQA Diamond | question | correct letter | 4 options (shuffled w/ fixed seed) | `category="gpqa"` |
| MMLU | question | letter | 4 options | `category=<subject>` ← drives the UX breakdown |
| GSM8K | problem | final number (after `####`) | — | `category="gsm8k"` |
| MATH-500 | problem | answer (contents of `\boxed{}`) | — | `category=<subject>`, `level` |
| HumanEval | prompt (function stub) | — | — | `test` (unit-test source), `entry_point`, `category="humaneval"` |
| IFEval | prompt | — | — | `instruction_id_list`, `kwargs` (the verifier args), `category="ifeval"` |

> ⚠️ **Licensing/citation:** record each dataset's source + license in the `DatasetSpec.description` and a
> `examples/benchmarks/README.md`. GSM8K (MIT), MMLU (MIT), GPQA (CC-BY, **gated** on HF — needs a token),
> MATH (MIT), HumanEval (MIT), IFEval (Apache-2.0). GPQA's gating means the converter needs `HF_TOKEN`.

### G2. Scorers

| Need | Plan | Effort |
|---|---|---|
| **Numeric math** (GSM8K) | Register a `numeric_answer` scorer = a thin prompt-and-extract: prefer Inspect core **`math()`** (handles `1,000`, fractions, equivalence) or `answer("line")`/`pattern(r"-?\$?\d[\d,]*\.?\d*")`. | tiny (wrap core) |
| **Symbolic math** (MATH-500) | Register **`math`** = wrap Inspect core `scorer.math()` directly. | tiny |
| **Code execution** (HumanEval) | New `code_exec` scorer: take the model's completion, assemble `prompt + completion + "\n" + metadata["test"] + f"\ncheck({entry_point})"`, run it in the sample's sandbox via `inspect_ai.util.sandbox().exec(["python","-c",src], timeout=…)`, pass iff exit 0. (This is exactly what `inspect_evals.humaneval` does — reimplement ~40 lines or vendor it.) | medium (sandbox) |
| **Instruction-following** (IFEval) | New `ifeval` scorer: port the [google-research IFEval](https://github.com/google-research/instruction_following_eval) verifier functions (or depend on `instruction_following_eval`), keyed by `metadata["instruction_id_list"]` + `kwargs`; report strict/loose prompt- & instruction-level accuracy. | medium (port verifiers) |
| **Model-graded** (open-ended) | Already have `llm_judge` (`model_graded_qa`). Nothing to add. | none |

All register in `builtins.py` with a `primary_metric` so the existing rollup/CI machinery just works.

### G3. Harnesses — and the "**a scorer can need a sandbox**" design point

- GPQA/MMLU → existing **`multiple_choice`**. GSM8K/MATH → existing **`single_turn`** (with a prompt
  nudge, see G4). IFEval → **`single_turn`**.
- **HumanEval exposes a real coupling gap.** Today the **sandbox is declared by the *harness*** (only
  `agentic` returns `(solver, sandbox)`; `runner._execute_batch` reads the 2-tuple). But HumanEval's
  *solver* is just `generate()` — the **sandbox is needed by the *scorer*** (to run the tests), not the
  solver. Two clean options:
  - **(a) A `code_generation` harness** that returns `(generate(), SandboxEnvironmentSpec(...))` — reuses
    the existing 2-tuple path verbatim; the runner provisions a sandbox the `code_exec` scorer then uses.
    *Lowest-risk; recommended.*
  - **(b)** Let the dataset declare a per-`Sample.sandbox` and have the runner attach a sandbox whenever
    any sample or scorer requests one. More general, but touches `_execute_batch`'s tuple logic.

  Document whichever we pick in `PLUGINS.md` (the harness/scorer contract) — it's a genuine extension of
  "the harness declares its sandbox."

### G4. Prompt templates / answer extraction (free-form evals)

MC is solved (the `multiple_choice` solver emits `ANSWER:`). For **GSM8K/MATH** the model must emit a
*parseable* final answer or the scorer can't extract it. Options, cheapest first: a fixed **instruction
suffix** baked into the converter's `input` ("Give your final answer as `ANSWER: <number>`" / "put your
final answer in `\boxed{}`"), **or** a `system_message` in a small `single_turn` config field. The
Inspect `math()` and `answer()` scorers are built around exactly these conventions, so aligning the prompt
to them is the whole trick. Add a `prompt_suffix` (or `system`) knob to `SingleTurnConfig` if we don't
want to bake it into the data.

### G5. Cost, model choice, budget (the < $10 constraint)

- **Pricing already works** for `openrouter/<id>` and gateway `openai/<id>` (catalog × tokens). Set a
  per-run `budget_usd` as a backstop; subsets keep us well under.
- **Model:** route a **cheap** model through the gateway — e.g. `anthropic/claude-3-5-haiku` (on-brand
  for an Anthropic-benchmark demo), or `openai/gpt-4o-mini` / `meta-llama/llama-3.1-8b-instruct` for the
  floor. Register one as a `ModelSpec` (e.g. `demo-cheap`) so the launch wizard offers it.
- See §6 for the per-benchmark budget table.

### G6. Entities + examples + dashboard wiring (the demo polish)

- A **seed script** (`tools/seed_benchmarks.py` or a `examples/benchmarks/*.yaml` set) that: registers
  each dataset (`POST /datasets`, gets snapshot+hash) → registers an `EvalSpec` per benchmark (dataset +
  default harness + default scorers) → registers the cheap `ModelSpec`. Then each benchmark is launchable
  from the dashboard's **"from registered eval"** drawer (#8, done) with a `limit`/budget — no YAML editing.
- Add `examples/benchmarks/<name>.yaml` RunSpecs for the CLI path too (mirrors `examples/mcq.yaml`).

---

## 4. Per-benchmark integration recipe (summary)

| Benchmark | Harness | Scorer(s) | New code needed |
|---|---|---|---|
| GPQA Diamond | `multiple_choice` | `choice` | converter only |
| MMLU (subjects) | `multiple_choice` | `choice` | converter only |
| GSM8K | `single_turn` (+ answer-suffix) | `numeric_answer` / core `math` | converter + wrap `math` scorer |
| MATH-500 | `single_turn` (+ `\boxed{}` suffix) | `math` (core) | converter + wrap `math` scorer |
| HumanEval | **`code_generation`** *(new)* | **`code_exec`** *(new)* | converter + harness + sandbox scorer |
| IFEval | `single_turn` | **`ifeval`** *(new)* | converter + verifier scorer |
| SWE-bench (demo) | `agentic` | bespoke patch/test scorer | large — **defer past the demo** |
| Open-ended (opt) | `single_turn` | `llm_judge` | converter only |

So the **net new plugin code** is just: wrap `math` (trivial), `code_exec` scorer + `code_generation`
harness (medium), `ifeval` scorer (medium). Everything else is **data conversion + registration**.

---

## 5. Build vs. depend on `inspect_evals` (decision)

| | Mine `inspect_evals`, ingest ourselves *(recommended)* | Depend on `inspect_evals` directly |
|---|---|---|
| Runs flow through our snapshot/ledger/analytics | ✅ (it's the demo) | ✗ fused Tasks bypass our compose path |
| Reproducible offline subsets | ✅ pinned JSONL in-repo | ✗ live HF pulls at run time |
| New code | ~3 small plugins + converters | adapter to run a foreign `Task` in `_execute_batch` |
| Battle-tested scorers (esp. code/ifeval) | copy ~40–80 lines each | reused as-is |

**Recommendation:** ingest data ourselves and **port the 2–3 scorers we lack** (cribbing from
`inspect_evals` / google-research IFEval for correctness). Optionally add `inspect_evals` as a *dev*
extra purely as a reference/oracle in tests (compare our subset scores against the upstream task on a
handful of samples).

---

## 6. Budget plan (target < $10 total)

Rough envelope on a **cheap** model (≈ $0.25–$1.00 per M input / $1.25–$5.00 per M output, i.e. Haiku-class;
gpt-4o-mini/llama-8b are ~10× cheaper still). Generous per-sample token assumptions:

| Benchmark | Subset | ~tok/sample (in+out) | Notes | Est. cost (Haiku-class) |
|---|---|---|---|---|
| GPQA Diamond | 50 | ~1.5k | long science Q + short CoT | < $0.50 |
| MMLU | 60 | ~0.8k | short MCQ | < $0.30 |
| GSM8K | 50 | ~1.0k | CoT to a number | < $0.40 |
| MATH-500 | 40 | ~2.0k | longer CoT | < $0.60 |
| HumanEval | 40 | ~1.2k | code gen (+ sandbox exec, **CPU not $**) | < $0.50 |
| IFEval | 50 | ~1.0k | format-constrained gen | < $0.40 |
| Open-ended (opt) | 20 | ~1.5k + judge calls | judge ≈ 1 extra cheap call/sample | < $0.50 |
| **Suite total** | ~310 | | single pass, `epochs=1` | **≈ $3–4** |

Headroom: even at **`epochs=3`** (for CIs) and a pricier mid-tier model the suite stays under $10. Each run
still carries a `budget_usd` backstop so a runaway can't blow the cap (it converts to `budget_skipped`).
HumanEval's sandbox exec is CPU/time, not model spend — bounded by the per-task `tool_timeout` and the
gVisor pool (#18).

---

## 7. Suggested implementation order

1. **Converters + data (G1)** — `tools/fetch_benchmark.py` for GPQA, MMLU, GSM8K, MATH-500; commit the
   subset JSONLs + `examples/benchmarks/README.md` (sources/licenses). *Unblocks 4 of 8 benchmarks with
   zero new plugin code besides one scorer.*
2. **Wrap the `math` scorer + `numeric_answer` (G2)** and add the answer-suffix knob (G4). → GSM8K + MATH
   green.
3. **Seed entities + dashboard (G6)** — register datasets/evals/model; verify launch-from-eval, the
   per-category MMLU breakdown, and model-vs-model compare render on real data. *This is the demo.*
4. **`code_generation` harness + `code_exec` scorer (G3/G2)** — HumanEval; reuses the agentic sandbox path
   and the done gVisor pool. Test locally with the Docker sandbox first.
5. **`ifeval` scorer (G2)** — port the verifiers; IFEval green.
6. **(optional)** open-ended `llm_judge` slice.
7. **SWE-bench Verified** — *separate effort*: 1–3 instances as an agentic/gVisor showcase; full scored
   integration deferred (note it in `FUTURE.md` if we want a tracked trigger).

## 8. Testing

- **Unit:** converter field-mapping + deterministic subsetting (same seed → same ids); each new scorer on
  a tiny fixture (a known-correct + known-wrong sample). Mirror the existing `test_multiple_choice_plugins`
  style.
- **Integration:** register → launch (mock model where possible) → assert ledger drains, analytics
  populated, per-`group_key` rollup non-empty. For `code_exec`, an e2e test needs the Docker sandbox
  (mark `e2e`, like the agentic tests).
- **Oracle (optional):** on ~10 samples, compare our subset score to `inspect_evals`' upstream task to
  catch mapping bugs.
- **Cost guard:** one real-model smoke run per benchmark with a small `limit` + tight `budget_usd`, assert
  finalize cost < the per-benchmark cell in §6.

---

## 9. Summary of concrete additions

**New files**
- `tools/fetch_benchmark.py` (+ `examples/benchmarks/*.jsonl`, `examples/benchmarks/README.md`)
- `tools/seed_benchmarks.py` (or `examples/benchmarks/*.yaml`)
- tests under `tests/unit` + `tests/integration` for converters/scorers

**`eval_engine/builtins.py`** — register: `math` (wrap core), `numeric_answer`, `code_exec` (sandbox),
`ifeval`; new `code_generation` harness; optional `prompt_suffix`/`system` on `SingleTurnConfig`.

**`eval_engine/datasets.py`** — *(only if we want a live `hf` source)* a `source="hf"` branch via
`inspect_ai.dataset.hf_dataset`. Otherwise untouched (converters feed the existing JSONL+snapshot path).

**`docs/PLUGINS.md`** — document the new harness/scorers and the "a scorer may declare a sandbox"
extension.

**No changes** to the ledger/orchestrator/worker/analytics spine — this is content + leaf plugins on a
finished platform.
