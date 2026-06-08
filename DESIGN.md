# Eval Engine — Design Doc (v1)

> This document describes the **current design**. Approaches we evaluated and dropped (and earlier
> decisions we reversed) live in **`docs/ALTERNATIVES.md`**; work deferred to a later trigger lives in
> **`docs/FUTURE.md`**. Companion deep-dives: `docs/ORCHESTRATION.md`, `docs/SCHEMA.md`,
> `docs/PLUGINS.md`, `docs/SANDBOXING.md`, `docs/SCHEDULER.md`. Design-review history is archived under
> `docs/design_review_history/`.

---

## 1. Overview

A modular, distributed engine for running **model evaluations** at scale, plus a web dashboard to
author, launch, monitor, and analyze runs. Core principle: **composability** — *model*, *harness*,
*dataset*, and *scorer* are independent blocks, and any valid combination works. That flexibility is
what makes it span QA, multi-turn agentic, LLM-as-judge, custom-code, and (later) human-scored evals.

### Goals
- Evaluate **any model × any harness × any dataset × any scorer**.
- Support eval shapes: single-turn QA, multiple-choice, multi-turn agentic (tool use), LLM-as-judge,
  programmatic/custom scoring, human review (schema now, UI later).
- **Distributed execution** of large runs with retries, rate-limit coordination, and resumability.
- **Durable, queryable metadata** for every run and sample.
- A **web dashboard** to manage, launch, monitor, and analyze.
- **Reproducibility of inputs:** a run's *inputs* are fully pinned by a versioned RunSpec + worker
  image digest; *outputs* are statistically comparable, not bitwise — hosted models are
  non-deterministic (§14).

### Non-goals (v1)
- Training/fine-tuning, RLHF data collection.
- Rebuilding a general experiment tracker (integrate, don't rebuild).
- Hosting models ourselves (we call them via APIs / our own inference servers).
- Hard multi-tenant isolation & per-team billing (schema is ready for it; enforcement later — `FUTURE.md`).

---

## 2. Scale envelope (the sizing all decisions are tuned to)

| Metric | Value |
|---|---|
| Samples per run | up to 100k (full); **subset runs much smaller** (fast iteration) |
| Runs per day | **~1000** |
| Concurrently *running* runs | low-tens, peaks ~50 |
| **Sample-evaluations / month** | **~1 billion** |
| Retention | 12 months |
| **Sample-results retained** | **~12 billion rows** |
| Avg sustained throughput | ~385 samples/s (bursty; higher at peak) |
| Workload | **genuine mix** of API providers + self-hosted (vLLM) |

> **Run-count reconciliation.** ~1000 runs/day (≈30k/month) reflects a workload **mix**, not a huge
> sample count: many runs are **small subsets for fast iteration**. A representative mix — ~300 full
> runs (~100k) + ~700 subset runs (~5k) per day ≈ 33.5M sample-evals/day ≈ **~1B/month** — keeps the
> two scale-forced figures intact: ClickHouse sizing (~1B/mo, ~12B retained) *and* claim QPS (~8
> claims/s at batch 50, which is why a plain claim suffices — §8). If iteration grows so subset runs
> become *additive* on top of the full-run budget, the figure to revisit is ClickHouse sizing — nothing
> in the control plane.

**The two numbers that force real infrastructure** are the **12B-row analytics store** and the
**global cross-worker rate limit**. Most other machinery is sized to those two; everything not forced
by them is kept minimal for v1 and deferred with a trigger (`FUTURE.md`).

Consequences that shaped the design:
- **Distribution is mandatory** — not a single box.
- **Global, cross-worker rate limiting is mandatory** (API quotas *and* self-hosted capacity).
- **ClickHouse is the analytics store from v1** (12B rows ≫ DuckDB's comfort zone; DuckDB stays the
  local-dev/spike path only).
- **The task ledger is ephemeral** (live runs only), not a 12B-row permanent table.
- **Transcript storage is the dominant cost** — addressed by sampling-by-default retention + tiering (§8).

---

## 3. Guiding principle: reuse Inspect AI as the kernel, build the platform around it

Building an eval *execution kernel* (dataset → solver → scorer, with tool use, sandboxing,
model-graded scoring, transcript logging) is a large solved problem. We **adopt
[Inspect AI](https://inspect.aisi.org.uk/) directly** (an approach we call *Inspect-native*) and spend our effort on the
differentiated platform: **distributed orchestration + central metadata store + dashboard.**

Why Inspect: modular `Dataset → Solver(=harness) → Scorer` abstractions that match our domain 1:1;
first-class agentic/tool-use + Docker/K8s sandboxing; built-in LLM-as-judge *and* arbitrary Python
scorers; provider-agnostic model layer; structured `.eval` logs + an official **viewer we embed**;
active development (UK AISI), becoming a de-facto standard. (Kernels we evaluated and rejected:
`ALTERNATIVES.md` §1.)

**Inspect-native means:** harnesses *are* Inspect `Solver`s, scorers *are* Inspect `Scorer`s, workers call
`inspect_ai` directly, and the `.eval` log is our **source-of-truth artifact**. We accept the coupling
and would only extract an interface if we ever truly need a non-Inspect backend.

> **Consistency rule:** Being Inspect-native does *not* make Inspect's log format our analytics store. Every completed
> sample is **flattened/ETL'd into ClickHouse** for querying. The `.eval` log is the immutable artifact
> in object storage; ClickHouse holds the queryable projection. That projection is indexing, not an
> abstraction layer, so it's consistent with staying Inspect-native.

---

## 4. Deployment & portability

- **Target: cloud Kubernetes** (greenfield, no existing infra).
- **First-class constraint: cloud portability** — "portable by interface, managed by choice":
  - **IaC:** Terraform (+ Helm), never CloudFormation/ARM. Same topology retargets EKS/GKE/AKS.
  - **Object storage:** standardize on the **S3 API** via an `fsspec`/boto-compatible abstraction → AWS
    S3, GCS S3-interop, or self-hosted **MinIO**. No native GCS/Blob APIs in app code.
  - **Postgres:** vanilla Postgres only (no Aurora-only features); CloudNativePG or any managed Postgres.
  - **ClickHouse:** Altinity operator on K8s, or (multi-cloud) ClickHouse Cloud.
  - **Redis** (rate-limit state): operator or any managed Redis (identical API everywhere).
  - **Secrets:** External Secrets Operator over cloud secret managers.
  - **Auth:** speak OIDC; back it with the org IdP (Google Workspace) or self-hosted Keycloak/Authentik.
- None of our components (Inspect, LiteLLM, FastAPI, Postgres, ClickHouse, MinIO, Next.js) are
  cloud-locked. The one cloud-specific surface — a KVM-capable sandbox node pool, *if* the microVM
  sandbox tier is ever built — is abstracted behind a Terraform node-pool module and a `RuntimeClass`
  (`FUTURE.md` §4).

---

## 5. Requirements

### Functional
FR1 Register & version datasets (HF/S3/JSONL/DB); slice/sample. · FR2 Define eval = dataset + harness +
scorer(s) + config, versioned. · FR3 Register models/targets + "model sets." · FR4 Launch run =
eval@version × model × harness-config × dataset-slice. · FR5 Distribute across workers; per-sample
retries; resume partial runs. · FR6 Global rate-limit & cost control per provider/model. · FR7 Persist
every sample's input, output, transcript, scores, tokens/cost, timing. · FR8 Aggregate metrics with
CIs; compare runs/models. · FR9 Dashboard: manage, launch, live-monitor, analyze, drill to transcript. ·
FR10 Reproduce a past run from its spec. · FR11 (later) Human review queue.

### Non-functional
Scale per §2 · crash-safe & resumable (no lost completed samples) · reproducible **inputs** (spec pins
eval code, dataset version, model id, params, seed, **worker image digest**); outputs comparable not
bitwise (§14) · extensible (add harness/scorer/provider without core changes) · observable (structured
logs, metrics) · cost-aware (token & $ per run/model/sample; budget caps).

---

## 6. Architecture

```
                       ┌─────────────────────────────────────────────┐
                       │                Web Dashboard                 │
                       │   Next.js: manage / launch / live-monitor    │
                       │   + canned ClickHouse analytics views        │
                       │   + embedded Inspect viewer (transcripts)    │
                       └───────────────────┬──────────────────────────┘
                                           │ REST / WebSocket (OIDC auth)
                       ┌───────────────────▼──────────────────────────┐
                       │           Control Plane API (FastAPI)         │
                       │  evals/datasets/models/runs CRUD, launch,     │
                       │  status, metrics proxy, auth, audit  (N repl.)│
                       └───────┬───────────────────────────┬───────────┘
                               │ write Run(queued)         │ read/write
                       ┌───────▼─────────┐          ┌──────▼──────────────┐
                       │  Orchestrator   │          │  Postgres            │
                       │  (1, leader-    │◄────────►│  metadata + EPHEMERAL│
                       │   elected):     │  ledger  │  sample-task ledger  │
                       │  admit→expand→  │          └─────────────────────┘
                       │  reconcile→     │
                       │  finalize       │
                       └───────┬─────────┘
                               │ (workers claim from the ledger)
                       ┌───────▼────────────────────────────────────────┐
                       │  Distributed Workers (K8s Deployment + KEDA)    │
                       │   Inspect Task per claimed shard                │
                       │             │ all model calls                   │
                       └─────────────┼──────────────────────────────────┘
                                     ▼
                       ┌──────────────────────────┐
                       │  LiteLLM gateway (≥2 repl)│──► API providers (OpenAI/Anthropic/…)
                       │  global rate-limit (Redis)│──► self-hosted vLLM (gateway-fronted)
                       │  cost/budget, fallback     │
                       └──────────────────────────┘
       results (async-insert) │            transcript │
          ┌──────────────────▼──┐   ┌────────────────▼─┐
          │     ClickHouse      │   │ Object store (S3/ │
          │  ~12B result rows   │   │ MinIO): .eval logs│
          │  + run_summary      │   │ + transcripts(zstd)│
          └─────────────────────┘   └──────────────────┘
```

**Two planes:** a lightweight **control plane** (API + Postgres + dashboard) for humans & state; a heavy
**execution plane** (Orchestrator + KEDA-scaled worker Deployment + kernel + gateway) for compute. They
talk only through Postgres + the ledger, so each scales independently. Workers do **not** coordinate —
the ledger (`FOR UPDATE SKIP LOCKED`) is the sole scheduler.

> The Orchestrator is a single **leader-elected** singleton (Postgres advisory lock) running one
> "derive state each tick" loop: admit runs, expand the dataset into the ledger, reconcile progress,
> enforce budget, finalize. It governs efficiency/fairness, not safety — its death just lets state go
> stale for a few seconds until the standby takes over; the durable ledger is untouched.

---

## 7. Core domain model

Keep these **orthogonal** (the composability guarantee):

- **Dataset** `{id, version, source, schema}` → **Samples** `{id, input, target?, metadata}`.
- **Target (Model)** `{provider, model_id, params, version}` — *what* we evaluate.
- **Harness (Solver)** `{type, config}` — *how* driven: `single_turn`, `multiple_choice`,
  `agentic(tools, max_steps, sandbox)`, `rag(retriever)`, custom.
- **Scorer** `{type, config}` — `programmatic`, `model_graded`, `match/regex`, **`human`** (enum present
  now; UI later). A run may have several.
- **Eval** `{id, version, dataset_ref, default_harness, default_scorers, config_schema,
  retention_policy}` — versioned bundle.
- **RunSpec** `{eval@version, target, harness_config, scorer_config, dataset_slice,
  sampling{n,seed,temperature…}, budget}` — the **fully reproducible unit**.
- **Run** `{id, run_spec, status, created_by, team, started/finished, aggregate_metrics, cost}` — one
  execution.
- **SampleResult** `{run_id, sample_id, output, transcript_ref, scores{}, tokens, cost, latency, error?,
  attempt, review_status}` — one evaluated sample.

Ownership fields (`created_by`, `team`) and `review_status` are present from day one so multi-tenancy
enforcement and the human-review queue are *additive* later, not migrations.

---

## 8. Component decisions

| Concern | Decision | Notes |
|---|---|---|
| Eval kernel | **Inspect AI — used directly (Inspect-native)** | solvers/scorers native; `.eval` = source-of-truth artifact |
| Transcript viewer | **Embedded Inspect viewer** | don't rebuild |
| Model gateway | **LiteLLM for ALL traffic** | external *and* self-hosted vLLM are gateway-fronted; ≥2 replicas; Redis global rate limits; per-`run_id` cost tally is canonical |
| Distribution | **K8s Deployment + KEDA** | stateless workers autoscaled on ledger queue depth (`count(queued)` + `maxReplicas` cap) |
| Coordination | **Postgres ephemeral task ledger** | skinny (status/lease/attempts/error only); claim/lease/retry/resume; the **sole** scheduler; pruned after a run completes |
| (escape hatch) | **pgmq / procrastinate** | drop-in durable-queue libs if the hand-rolled lease proves bug-prone |
| Metadata DB | **Postgres** | runs/specs/entities + live ledger |
| Artifacts | **S3/MinIO** | `.eval` logs + transcripts, **zstd**; **sample-by-default retention** + storage-class tiering |
| Analytics | **ClickHouse** | ~12B rows; workers async-insert directly; `run_summary` at finalize; DuckDB = dev/spike only |
| Control plane | **Python / FastAPI** | one backend language, shared types with workers |
| Dashboard | **Next.js** + **canned CH-backed analytics views** | custom manage/launch/monitor + embedded Inspect viewer |
| Admission/scheduling | **Two-lane (interactive/batch) + fixed per-run cap** | borrowable interactive reserve; no per-tick allocator (`SCHEDULER.md`) |
| Auth | **Google OIDC/SSO** | `created_by`/`team`, admin/member, audit, shared visibility |
| Human review | **schema ready, UI deferred** | `human` scorer + `review_status` |
| IaC / portability | **Terraform + Helm, S3 API, vanilla PG** | portable by interface |

**Key mechanisms (detail in the companion docs):**
- **Skinny ledger + plain claim.** `sample_tasks` carries only coordination. The claim is the plain
  `FOR UPDATE SKIP LOCKED` over `ORDER BY sample_id`, bounded by a per-run `max_inflight`, with
  `not_before` for poison-sample backoff. ~8 claims/s at peak — a plain claim is ample (`SCHEMA.md`,
  `ORCHESTRATION.md`).
- **Commit protocol (ack-before-flip).** Per result: write transcript → S3 (idempotent key) → async-insert
  to ClickHouse and **block for a durable ack** (`wait_for_async_insert=1`) → **then** flip the ledger row
  `done`. Invariant: `done` ⟹ the result is durable in ClickHouse, so a crash never loses a completed
  sample. Atomicity becomes *ordering + idempotency* (`ORCHESTRATION.md` §5).
- **Exactly-once analytics.** `ReplacingMergeTree` keyed `(run_id, sample_id)` (version = load time): a
  re-execution's newer result wins, a duplicate re-insert collapses. Headline metrics are computed **once
  at finalize** into `run_summary` (`SCHEMA.md`).
- **Live metrics.** The Orchestrator writes the live rollup to the Postgres `runs` row — progress (ledger
  status counts) + cost (gateway tally) each tick, and a live score (`avg(passed) FROM sample_results
  WHERE eval_id=E AND target_id=T AND run_id=X`, the **full sort-key prefix**, ~once/minute, no `FINAL`).
  Clients read live *and* final from `runs`; `run_summary` is the finalize record.
- **Budget = terminal class.** A gateway budget reject is a distinct terminal `BudgetExceeded` signal
  (not a 429/retry), so it never burns attempts or inflates `failed_samples`. The gateway's per-`run_id`
  tally is the single source of cost truth (`ORCHESTRATION.md` §8).
- **Execution granularity.** A worker claims a **shard** and runs it as one Inspect Task; shard size
  defaults from harness type (large for fast/uniform QA, down to 1 for high-variance agentic). The per-run
  `.eval` artifact is a collection of per-shard logs (`ORCHESTRATION.md`).

---

## 9. Execution flow (a run, end to end)

1. User defines/loads an **Eval**, picks a model set + config → `POST /runs` (OIDC user).
2. Control plane validates against the eval's config schema; writes **RunSpec** + **Run(queued)** with
   `created_by`/`team`.
3. Orchestrator **admits** the run (two-lane interactive/batch admission) and **expands** the dataset
   slice into **sample-tasks** in the ephemeral ledger (queued).
4. Workers atomically **claim** a shard; each runs the Inspect solver (model calls via LiteLLM) then
   scorer(s).
5. Per sample: transcript (zstd) → object store; result **async-inserted** to ClickHouse with a durable
   ack; **then** the ledger row flips `done`. `.eval` shard logs → object store. Failures retry with
   backoff to N; permanent failures recorded.
6. Crash safety: a dead worker's `running` tasks expire by lease and are reclaimed; resume = "tasks not
   done." Re-execution is idempotent (ReplacingMergeTree dedups).
7. On completion: Orchestrator computes aggregates + cost into `run_summary`/`runs`; **archives failed
   tasks and prunes the ledger** (state now lives in ClickHouse/object store).
8. Analyze via canned ClickHouse views (compare/slice/CIs) + Inspect viewer (per-sample drill-down).
9. Reproduce: "re-run" clones the RunSpec → new Run with identical pinned inputs.

---

## 10. Recommended stack (TL;DR)

Python kernel **Inspect AI** · **LiteLLM** gateway (all traffic) · **K8s Deployment + KEDA** execution ·
**Postgres** metadata + ephemeral ledger · **FastAPI** control plane · **S3/MinIO** artifacts (zstd) ·
**ClickHouse** analytics · **Next.js** dashboard + **embedded Inspect viewer** + **canned CH views** ·
**Google OIDC** · **Terraform + Helm** on cloud **Kubernetes**, portable by interface.

---

## 11. Roadmap (current state)

- **Phase 0 (done):** Inspect spike — QA + agentic eval, results → Postgres + ClickHouse(flatten).
- **Phase 1:** single-node platform (FastAPI + Postgres + minimal Next.js + embedded viewer + LiteLLM +
  OIDC).
- **Phase 2:** distribution (KEDA worker Deployment + ephemeral ledger + retries/resume + global rate
  limiting).
- **Phase 3:** analytics (ClickHouse + canned views; comparison, CIs, regression, cost).
- **Phase 4+ and trigger-gated subsystems:** see **`docs/FUTURE.md`**.

---

## 12. Risks
- **Agentic sandbox churn (top-tier).** A fresh pod per sample can't sustain hundreds/s — K8s
  control-plane ceilings (scheduler/kubelet/IPAM/gVisor-boot) are low-hundreds/s. v1 ships hardened
  air-gapped per-sample pods (fine while agentic is a minority of the 385/s aggregate); the pooled
  microVM sandbox service is the trigger-gated mitigation (`SANDBOXING.md`, `FUTURE.md` §4). **Gate
  agentic-at-scale on a churn spike.**
- **Global rate-limit correctness** under high concurrency — load-test the LiteLLM+Redis path.
- **Ledger claim/lease/idempotency** — a *thin* surface (skinny ledger); a wrongful reclaim is wasted
  spend, not corruption (idempotent CH writes). `pgmq` is the escape hatch.
- **Transcript storage growth** — addressed by sample-by-default retention + storage tiering.
- **Inspect coupling** (the Inspect-native bet) — accepted; the ClickHouse projection keeps analytics insulated.
- **ClickHouse ops** at 12B rows — partitioning/TTL design up front.

---

## 13. Decisions (current)

- **Scale:** ~1000 runs/day · up to 100k samples/run · 12-mo retention → ~1B samples/mo, ~12B retained.
  Mixed API + self-hosted.
- **Deployment:** cloud Kubernetes, greenfield, **portable by interface** (Terraform/Helm, S3 API,
  vanilla PG). ClickHouse from v1.
- **Kernel:** **Inspect-native** — adopt Inspect directly; `.eval` = source-of-truth; flatten to ClickHouse.
- **Coordination:** **K8s Deployment + KEDA** over the **Postgres ephemeral ledger** (sole scheduler);
  `pgmq`/`procrastinate` escape hatch.
- **Gateway:** **LiteLLM for all traffic** (external + gateway-fronted vLLM); per-`run_id` cost tally is
  canonical; horizontally scaled, Redis global limits.
- **Auth/tenancy:** SSO/OIDC (Google), ownership + admin/member + audit, shared visibility, tenancy-ready
  schema.
- **Dashboard:** Next.js custom manage/launch/monitor + embedded Inspect viewer + canned CH-backed views.
- **Transcript retention:** **sample-by-default** (keep all failures + a stratified sample of passes;
  per-eval opt-in `keep_all`) + storage-class tiering; zstd.
- **Control-plane language:** Python/FastAPI.
- **Human-in-the-loop:** schema ready, UI deferred.
- **Admission/scheduling:** two-lane interactive/batch + fixed per-run cap (no fair-share allocator in
  v1).
- **Dataset versioning:** content-addressed immutable snapshots in object storage + a Postgres pointer.

Decisions we reversed along the way (and why) are recorded in **`docs/ALTERNATIVES.md`**.

---

## 14. Reproducibility (inputs pinned, outputs comparable)

Hosted models are non-deterministic and server-versioned; `seed`/`temp=0` don't guarantee bitwise
reproducibility; judges are non-deterministic; model IDs deprecate inside the retention window. So our
contract is **inputs fully pinned, outputs statistically comparable**:

- **Inputs pinned** by a versioned RunSpec: eval code (`code_ref`), dataset version (content hash), model
  id + params + seed, **worker image digest**, and a **provider version-fingerprint** recorded on the run.
- **Outputs comparable** via **epochs** (repeat samples) for statistical comparability, not bitwise
  equality. Self-hosted vLLM (pinnable weights) is genuinely more reproducible than hosted APIs.

Reproduce = "re-run" clones the RunSpec → a new Run with identical pinned inputs.
