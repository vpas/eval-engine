# Eval Engine — Design Doc (v0.2)

> Status: **decisions locked from our v0.1 walkthrough.** All 10 open questions resolved
> (§13 decision log). This version bakes them into the architecture. Remaining open items
> are smaller and listed in §14.

---

## 1. Overview

A modular, distributed engine for running **model evaluations** at scale, plus a web
dashboard to author, launch, monitor, and analyze runs. Core principle: **composability** —
*model*, *harness*, *dataset*, and *scorer* are independent blocks, and any valid
combination works. That's what makes it flexible across QA, multi-turn agentic,
LLM-as-judge, custom-code, and (later) human-scored evals.

### Goals
- Evaluate **any model × any harness × any dataset × any scorer**.
- Support eval shapes: single-turn QA, multiple-choice, multi-turn agentic (tool use),
  LLM-as-judge, programmatic/custom scoring, human review (schema now, UI later).
- **Distributed execution** of large runs with retries, rate-limit coordination,
  checkpointing, and resumability.
- **Durable, queryable metadata** for every run and sample.
- A **web dashboard** to manage, launch, monitor, and analyze.
- **Reproducibility**: a run is fully described by a versioned RunSpec.

### Non-goals (v1)
- Training/fine-tuning, RLHF data collection.
- Rebuilding a general experiment tracker (integrate, don't rebuild).
- Hosting models ourselves (we call them via APIs / our own inference servers).
- Hard multi-tenant isolation & per-team billing (schema is ready for it; enforcement later).

---

## 2. Scale envelope (the sizing all decisions are tuned to)

| Metric | Value |
|---|---|
| Samples per run | 100k |
| Runs per month | 10k |
| **Sample-evaluations / month** | **~1 billion** |
| Retention | 12 months |
| **Sample-results retained** | **~12 billion rows** |
| Avg sustained throughput | ~385 samples/s (bursty; higher at peak) |
| Workload | **genuine mix** of API providers + self-hosted (vLLM) |

Consequences that shaped the design:
- **Distribution is mandatory** — not a single box.
- **Global, cross-worker rate limiting is mandatory** (API quotas *and* self-hosted capacity).
- **ClickHouse is the analytics store from v1** (12B rows ≫ DuckDB's comfort zone; DuckDB
  stays as the local-dev/spike path only).
- **The task ledger is ephemeral** (live runs only), not a 12B-row permanent table.
- **Transcript storage is the dominant cost** — addressed by §6.4 + Decision D8.
- **The model gateway must be horizontally scaled** (thousands of calls/s at peak).

---

## 3. Guiding principle: reuse Inspect AI as the kernel, build the platform around it

Building an eval *execution kernel* (dataset → solver → scorer, with tool use, sandboxing,
model-graded scoring, transcript logging) is a large solved problem. We **adopt
[Inspect AI](https://inspect.aisi.org.uk/) directly** (Decision D3 = "Pure A") and spend our
effort on the differentiated platform: **distributed orchestration + central metadata store +
dashboard.**

Why Inspect: modular `Dataset → Solver(=harness) → Scorer` abstractions that match our
domain 1:1; first-class agentic/tool-use + Docker sandboxing; built-in LLM-as-judge *and*
arbitrary Python scorers; provider-agnostic model layer; structured `.eval` logs + an
official **viewer we embed**; active development (UK AISI), becoming a de-facto standard.

**Pure A means:** harnesses *are* Inspect `Solver`s, scorers *are* Inspect `Scorer`s,
workers call `inspect_ai` directly, and the `.eval` log is our **source-of-truth artifact**.
We accept the coupling and would only extract an interface if we ever truly need a
non-Inspect backend.

> **Important consistency rule:** Pure A does *not* make Inspect's log format our analytics
> store. Every completed sample is **flattened/ETL'd into ClickHouse** for querying. The
> `.eval` log is the immutable artifact in object storage; ClickHouse holds the queryable
> projection. This projection is indexing, not an abstraction layer, so it's consistent
> with Pure A.

**Alternatives considered:** lm-evaluation-harness (benchmark-centric, weak agentic),
OpenAI Evals (low activity), HELM (heavyweight), promptfoo (TS, not 10⁶-scale),
DeepEval/Ragas (narrower), build-from-scratch (largest cost). Inspect wins as the kernel.

---

## 4. Deployment & portability

- **Target: cloud Kubernetes** (greenfield, no existing infra), with **KubeRay** for Ray.
- **First-class constraint: cloud portability** — "portable by interface, managed by choice":
  - **IaC:** Terraform (+ Helm), never CloudFormation/ARM. Same topology retargets EKS/GKE/AKS.
  - **Object storage:** standardize on the **S3 API** via an `fsspec`/boto-compatible
    abstraction → AWS S3, GCS S3-interop, or self-hosted **MinIO**. No native GCS/Blob APIs in app code.
  - **Postgres:** vanilla Postgres only (no Aurora-only features); CloudNativePG operator or any managed Postgres behind one connection string.
  - **ClickHouse:** Altinity operator on K8s, or (multi-cloud) ClickHouse Cloud.
  - **Redis** (rate-limit state + queue): operator or any managed Redis (identical API everywhere).
  - **Secrets:** External Secrets Operator as the abstraction over cloud secret managers.
  - **Auth:** speak OIDC; back it with the org IdP or self-hosted Keycloak/Authentik.
- None of our components (Inspect, Ray, LiteLLM, FastAPI, Postgres, ClickHouse, MinIO,
  Langfuse, Superset, Next.js) are cloud-locked.

---

## 5. Requirements

### Functional
FR1 Register & version datasets (HF/S3/JSONL/DB); slice/sample. · FR2 Define eval =
dataset + harness + scorer(s) + config, versioned. · FR3 Register models/targets +
"model sets." · FR4 Launch run = eval@version × model × harness-config × dataset-slice. ·
FR5 Distribute across workers; per-sample retries; resume partial runs. · FR6 Global
rate-limit & cost control per provider/model. · FR7 Persist every sample's input, output,
transcript, scores, tokens/cost, timing. · FR8 Aggregate metrics with CIs; compare runs/
models. · FR9 Dashboard: manage, launch, live-monitor, analyze, drill to transcript. ·
FR10 Reproduce a past run from its spec. · FR11 (later) Human review queue.

### Non-functional
Scale per §2 · crash-safe & resumable (no lost completed samples) · reproducible
(spec pins eval code, dataset version, model id, params, seed) · extensible (add harness/
scorer/provider without core changes) · observable (structured logs, per-call traces,
metrics) · cost-aware (token & $ per run/model/sample; budget caps).

---

## 6. Architecture

```
                       ┌─────────────────────────────────────────────┐
                       │                Web Dashboard                 │
                       │   Next.js: manage / launch / live-monitor    │
                       │   + embedded Inspect viewer (transcripts)    │
                       │   + Superset (analytics over ClickHouse)     │
                       └───────────────────┬──────────────────────────┘
                                           │ REST / WebSocket (OIDC auth)
                       ┌───────────────────▼──────────────────────────┐
                       │           Control Plane API (FastAPI)         │
                       │  evals/datasets/models/runs CRUD, launch,     │
                       │  status, metrics proxy, auth, audit           │
                       └───────┬───────────────────────────┬───────────┘
                               │ enqueue run               │ read/write
                       ┌───────▼─────────┐          ┌──────▼──────────────┐
                       │  Orchestrator   │          │  Postgres            │
                       │  expand→shard,  │◄────────►│  metadata + EPHEMERAL│
                       │  lifecycle FSM, │  ledger  │  sample-task ledger  │
                       │  retries/resume │          └─────────────────────┘
                       └───────┬─────────┘
                               │ dispatch sample-tasks
                       ┌───────▼────────────────────────────────────────┐
                       │            Distributed Workers (Ray)            │
                       │   Inspect solver+scorer per sample (Pure A)     │
                       │             │ all model calls                   │
                       └─────────────┼──────────────────────────────────┘
                                     ▼
                       ┌──────────────────────────┐
                       │  LiteLLM proxy (≥2 repl.) │──► API providers (OpenAI/Anthropic/…)
                       │  global rate-limit (Redis)│──► self-hosted vLLM (load-balanced)
                       │  cost/budget, fallback     │
                       └──────────────────────────┘
                 results │            traces │
          ┌──────────────▼──────┐   ┌────────▼─────────┐
          │ Object store (S3/   │   │  Langfuse        │
          │ MinIO): .eval logs, │   │  (LLM tracing)   │
          │ transcripts (zstd)  │   └──────────────────┘
          └──────────┬──────────┘
                     │ flatten/ETL per sample
          ┌──────────▼──────────┐
          │     ClickHouse      │  ◄── Superset queries (analytics, compare, CIs)
          │  ~12B result rows   │
          └─────────────────────┘
```

**Two planes:** lightweight **control plane** (API + Postgres + dashboard) for humans &
state; heavy **execution plane** (orchestrator + Ray + kernel + gateway) for compute. They
talk only through Postgres + the queue, so each scales independently.

---

## 7. Core domain model

Keep these **orthogonal** (the composability guarantee):

- **Dataset** `{id, version, source, schema}` → **Samples** `{id, input, target?, metadata}`.
- **Target (Model)** `{provider, model_id, params, version}` — *what* we evaluate.
- **Harness (Solver)** `{type, config}` — *how* driven: `single_turn`, `multiple_choice`,
  `agentic(tools, max_steps, sandbox)`, `rag(retriever)`, custom.
- **Scorer** `{type, config}` — `programmatic`, `model_graded`, `match/regex`, **`human`**
  (enum present now; UI later). A run may have several.
- **Eval** `{id, version, dataset_ref, default_harness, default_scorers, config_schema,
  retention_policy=keep_all}` — versioned bundle.
- **RunSpec** `{eval@version, target, harness_config, scorer_config, dataset_slice,
  sampling{n,seed,temperature…}, budget}` — the **fully reproducible unit**.
- **Run** `{id, run_spec, status, created_by, team, started/finished, aggregate_metrics,
  cost}` — one execution.
- **SampleResult** `{run_id, sample_id, output, transcript_ref, scores{}, tokens, cost,
  latency, error?, attempt, review_status}` — one evaluated sample.

Ownership fields (`created_by`, `team`) and `review_status` are present from day one so
multi-tenancy enforcement and the human-review queue are *additive* later, not migrations.

---

## 8. Component decisions (locked)

| Concern | Decision | Notes |
|---|---|---|
| Eval kernel | **Inspect AI, Pure A** (D3) | solvers/scorers native; `.eval` = source-of-truth artifact |
| Transcript viewer | **Embedded Inspect viewer** | don't rebuild |
| Model gateway | **LiteLLM proxy, single egress** (D5) | API + self-hosted; ≥2 replicas; Redis global limits; per-run cost/budget |
| Distribution | **Ray (KubeRay)** (D4) | embarrassingly parallel sample fan-out |
| Durability | **Postgres ephemeral task ledger** (D4) | claim/lease/retry/resume; pruned after run completes |
| (escape hatch) | Temporal | only if run-level orchestration grows multi-step |
| Metadata DB | **Postgres** | runs/specs/entities + live ledger |
| Artifacts | **S3/MinIO** | `.eval` logs + transcripts, **zstd**, **keep-all 12mo** (D8) |
| Analytics | **ClickHouse** (D2-scale) | ~12B rows; DuckDB = dev/spike only |
| Tracing | **Langfuse** (OSS, self-host) | per-call traces, cost, latency |
| Control plane | **Python / FastAPI** (D9) | one backend language, shared types w/ workers |
| Dashboard | **Next.js**; **Superset** for analytics (D7) | custom manage/launch/monitor; Superset over ClickHouse |
| Auth | **OIDC/SSO** (D6) | `created_by`/`team`, admin/member, audit, shared visibility |
| Human review | **schema ready, UI deferred** (D10) | `human` scorer + `review_status` |
| IaC / portability | **Terraform + Helm, S3 API, vanilla PG** (D2) | portable by interface |

---

## 9. Execution flow (a run, end to end)

1. User defines/loads an **Eval**, picks a model set + config → `POST /runs` (OIDC user).
2. Control plane validates against the eval's config schema; writes **RunSpec** + **Run(queued)** with `created_by`/`team`.
3. Orchestrator expands the dataset slice into **sample-tasks** in the **ephemeral ledger** (queued).
4. Ray workers atomically **claim** tasks; each runs the Inspect solver (model calls via LiteLLM) then scorer(s).
5. Per sample: `.eval` log + transcript (zstd) → object store; trace → Langfuse; flattened
   result → ClickHouse; ledger row → done. Failures retried w/ backoff to N; permanent failures recorded.
6. Crash safety: a dead worker's `running` tasks expire by lease and are reclaimed; resume = "tasks not done."
7. On completion: orchestrator computes aggregates + cost; **prunes the ledger** (state now lives in ClickHouse/object store).
8. Analyze via Superset (compare/slice/CIs) + Inspect viewer (per-sample drill-down).
9. Reproduce: "re-run" clones the RunSpec → new Run with identical pinned inputs.

---

## 10. Recommended stack (TL;DR)

Python kernel **Inspect AI** · **LiteLLM** single-egress gateway · **Ray/KubeRay** execution ·
**Postgres** metadata + ephemeral ledger · **FastAPI** control plane · **S3/MinIO** artifacts
(zstd) · **ClickHouse** analytics · **Langfuse** tracing · **Next.js** dashboard +
**embedded Inspect viewer** + **Superset** · **Keycloak/Authentik or org IdP** (OIDC) ·
**Terraform + Helm** on cloud **Kubernetes**, portable by interface.

---

## 11. Phased roadmap

- **Phase 0 — Spike:** Inspect locally; 1 QA + 1 agentic eval; 1 model; results → Postgres +
  ClickHouse(flatten). Prove kernel + data model + the ETL projection.
- **Phase 1 — Single-node platform:** FastAPI + Postgres + minimal Next.js (launch/list/
  monitor) + embedded Inspect viewer; LiteLLM proxy in front; OIDC login. No distribution yet.
- **Phase 2 — Distribution:** KubeRay fan-out + Postgres ephemeral ledger + retries/resume +
  global rate limiting; scale to large runs.
- **Phase 3 — Analytics:** ClickHouse + Superset; model-comparison, CIs, regression tracking,
  cost dashboards.
- **Phase 4 — Hardening:** Langfuse, budgets/alerts, audit, retention policy (flip from
  keep-all), then human-review queue / tenancy enforcement as needed.

---

## 12. Risks
- **Global rate-limit correctness** under high concurrency — load-test the LiteLLM+Redis path.
- **Ledger claim/lease/idempotency** semantics — core infra; design + test carefully.
- **Transcript storage growth** under keep-all — mitigated short-term by zstd; retention is
  a known later lever (D8), schema already `retention_policy`-aware.
- **Inspect coupling** (Pure A) — accepted; ClickHouse projection keeps analytics insulated.
- **Agentic sandbox isolation** — Docker-on-K8s is insufficient; use Inspect's **k8s sandbox
  provider** with per-sample ephemeral pods + tiered isolation (gVisor/Kata), air-gap by
  default, deny-all egress. Full model in **docs/SANDBOXING.md**.
- **ClickHouse ops** at 12B rows — partitioning/TTL design up front.

---

## 13. Decision log (resolved from v0.1 §12)

- **D1 Scale:** 100k samples/run · 10k runs/mo · 12-mo retention → ~1B samples/mo, ~12B retained. Mixed API + self-hosted.
- **D2 Deployment:** cloud Kubernetes, greenfield, **portable by interface** (Terraform/Helm, S3 API, vanilla PG). ClickHouse promoted to v1.
- **D3 Kernel:** **Pure A** — adopt Inspect directly; `.eval` = source-of-truth; flatten to ClickHouse for analytics.
- **D4 Orchestration:** **Ray + Postgres ephemeral ledger**; Temporal = documented escape hatch.
- **D5 Gateway:** **LiteLLM single egress** for all traffic (API + self-hosted); horizontally scaled; per-run cost/budget.
- **D6 Auth/tenancy:** **(b)** SSO/OIDC, ownership + admin/member + audit, shared visibility, tenancy-ready schema. IdP pluggable.
- **D7 Dashboard:** **(b) + Superset** — custom manage/launch/monitor, embedded Inspect viewer, Superset analytics.
- **D8 Transcript retention:** **keep-all 12 mo** for simplicity; zstd compression + per-eval `retention_policy` default `keep_all` so tiering is additive later.
- **D9 Control-plane language:** **Python/FastAPI**.
- **D10 Human-in-the-loop:** **defer UI, schema ready** (`human` scorer type, `review_status`).

---

## 14. Remaining open items (smaller, for v0.3)
1. ~~**IdP**~~ — **RESOLVED**: **Google OIDC** (Google Workspace identities) as the auth provider.
2. **Dataset versioning mechanism**: content-hash + pointer in Postgres, vs DVC/LakeFS, vs HF datasets revisions?
3. **Concrete schemas**: Postgres DDL (entities + ledger) and ClickHouse table (partitioning/TTL/ordering keys).
4. **Harness/scorer plugin registration**: how custom harnesses/scorers are packaged & discovered (entry points? a registry?).
5. **Budget enforcement semantics**: hard-stop vs warn at run/model/team budget thresholds.
6. **Run cancellation semantics**: drain in-flight vs hard-kill; partial-result handling.
7. **Secrets for self-hosted endpoints**: how worker→vLLM auth/discovery works through the gateway.

*Next: I can (a) draft the §14.3 concrete schemas, (b) sketch the harness/scorer plugin
interface, or (c) detail the orchestrator/ledger state machine. Say which to tackle first.*
