# Eval Engine — Alternatives Considered & Decisions Reversed

> Companion to `DESIGN.md`. This is the **archive**: approaches we evaluated and did **not**
> adopt, and earlier decisions we later **reversed**. The main docs describe only the current
> design; the *reasoning for what we rejected* lives here so the current docs stay clean without
> losing the "why not". Deferred-but-planned work is in `docs/FUTURE.md` (different bucket: those
> we *will* build on a trigger; these we do not intend to build).

---

## 1. Eval kernel — alternatives to Inspect AI

We adopt **Inspect AI directly** (*Inspect-native* — harnesses/scorers are its own `Solver`/`Scorer`
types, not a wrapper over them). Rejected:

| Option | Why not |
|---|---|
| **lm-evaluation-harness** | Benchmark-centric; weak agentic/tool-use story. |
| **OpenAI Evals** | Low activity; narrower. |
| **HELM** | Heavyweight; awkward to embed as a kernel. |
| **promptfoo** | TypeScript; not built for 10⁶-scale runs. |
| **DeepEval / Ragas** | Narrower (RAG/unit-test framing). |
| **Build from scratch** | Largest cost; the kernel (dataset→solver→scorer, tool use, sandboxing, model-graded scoring, transcript logging) is a solved problem. |

---

## 2. Distribution — Ray / KubeRay (reversed)

**Originally:** Ray (KubeRay) for worker fan-out. **Reversed → plain K8s Deployment + KEDA.**

The prototype proved workers **never coordinate** — the Postgres ledger (`FOR UPDATE SKIP LOCKED`)
is the sole scheduler, so Ray's DAG / object-store / locality features went unused. A Deployment +
KEDA does the only thing we used Ray for — scale 0↔N — with far less operational weight, and scales
to zero between runs. Ray would have added a head node, an object store, and a second scheduler with
nothing to schedule.

---

## 3. Durable queue / workflow engine — Temporal, SQS (rejected)

We keep a **thin hand-rolled Postgres claim/lease** (with `pgmq`/`procrastinate` named as a drop-in
escape hatch). Rejected **Temporal** (and SQS-style queues) as the primary coordinator:

- **Wrong shape.** Temporal is a multi-step *workflow* engine; our work is a **single-step fan-out**
  (claim → run one sample → record). We'd use almost none of it.
- **Same bottleneck, relocated.** Temporal's own persistence becomes the same ~1B/month write
  problem we already solve in Postgres + ClickHouse.
- **Can't answer our questions.** Our control plane asks **SQL-shaped** questions (counts by status,
  fairness, budget) over the ledger — a queue can't.

The ledger is our *domain model* that happens to use a standard claim pattern, not a generic queue
we're reinventing. `pgmq` stays the escape hatch if the hand-rolled lease ever proves bug-prone.

---

## 4. Result path — ResultLoader + widened ledger rows (reversed)

**Originally:** workers wrote fat result payloads into the ledger row, and a separate **ResultLoader**
batch-copied them to ClickHouse. **Reversed → skinny ledger + workers async-insert directly to
ClickHouse.**

Co-locating tiny hot coordination updates (status/lease) with fat append-mostly result columns on one
high-churn table caused **dead-tuple / autovacuum bloat** at 1B/month. The skinny ledger carries only
coordination; results go straight to ClickHouse with server-side async-insert buffering. The
ResultLoader is gone — one fewer component, no loader-sharding problem.

---

## 5. Headline metrics — insert-time AggregatingMergeTree MV (rejected)

**Considered:** an insert-time `AggregatingMergeTree` materialized view maintaining per-run
`pass_rate`/`cost`. **Rejected → compute once at finalize into `run_summary`.**

An MV fires per insert block and **never sees the later `ReplacingMergeTree` collapse** — so a
re-loaded or re-executed batch is deduped in the base table but **double-counted** in the MV (the
headline numbers). Computing metrics once at finalize over the deduped run partition (`FINAL`) is
exactly-once by construction.

---

## 6. Cost accounting — worker-recomputed catalog price (reversed)

**Originally:** workers computed cost from token counts × a catalog price, alongside the gateway's own
tally. **Reversed → the LiteLLM gateway's per-`run_id` tally is the single source of truth.** Two
independent cost computations inevitably diverge; the gateway sees actual billed usage. Workers stopped
recomputing cost. (The prototype's `runner._cost_usd` is a gateway stand-in for local runs only.)

---

## 7. Claim ordering — hash-sharded claim as the default (reversed)

**Originally:** the claim was **hash-sharded** (`AND hashtext(sample_id) % N = shard`) by default to
avoid a thundering herd on the ordered queue head. **Reversed → plain ordered claim + `not_before`.**

The thundering herd is driven by claim QPS = `throughput / batch_size` — only ~8 claims/s at batch 50,
with <1 concurrent claim per run in realistic configs. `not_before` already removes the poison-sample
head-of-line. Hash-sharding only matters in a different workload shape (batch≈1, sub-second samples,
thousands of single-run claimers) — so it's a **deferred, purely-additive** optimization, not a
default (see `docs/FUTURE.md`).

---

## 8. Transcript retention — keep-all-12-months (reversed)

**Originally:** keep every transcript for the full 12-month retention, "for simplicity."
**Reversed → sample-by-default** (keep all failures + a stratified sample of passes; per-eval opt-in
`keep_all`) + storage-class tiering.

Transcript storage is the **named dominant cost**, and the asymmetry is the point: keep-all's sunk
storage cost is **not reversible downward**, while sampling is **reversible upward** (you can always
keep more later). The ClickHouse projection stays full-fidelity regardless.

---

## 9. Analytics slicing — single concatenated `group_key` (reversed)

**Originally:** a single concatenated `group_key` string for dimensional slicing, sitting in the
immutable `ORDER BY`. **Reversed → `dimensions Map(String, LowCardinality(String))` + a first-class
`category`.** A concatenated key kills independent multi-dimension slicing and is an early-binding trap
in the immutable sort key. The `ORDER BY` is now keyed on stable **access patterns** (eval/run); hot
map dimensions are promoted to materialized columns later **without a sort-key rewrite**.

---

## 10. Live accuracy read — `FINAL` on the live read (reversed)

**Briefly adopted** (a prior review): add `FINAL` to the live in-progress score read "to eliminate
un-merged-duplicate skew at ~zero cost." **Reversed → drop `FINAL` on the live read, throttle to
~once/minute; finalize keeps `FINAL`.** At single-run scale `FINAL` was ~free; at ~50 concurrent runs a
per-tick merge-on-read across every run is not. The tiny, transient live skew is the accepted trade for
a *live gauge*; the finalize `run_summary` stays exact. (Both reads use the full `(eval_id, target_id,
run_id)` sort-key prefix — see `SCHEMA.md`.)

---

## 11. Sandbox runtime — Docker-in-Docker / host Docker socket (rejected)

Inspect's default Docker sandbox needs a Docker daemon. On Kubernetes that means **Docker-in-Docker
(privileged)** or **mounting the host Docker socket** — privileged-container anti-patterns that
*undermine the isolation we're buying*. **Rejected → Inspect's Kubernetes sandbox provider** (ephemeral
pod per sample), then hardened.

---

## 12. Dataset versioning — LakeFS / DVC / HF revisions (not adopted as system-of-record)

We use **content-addressed immutable snapshots in object storage + a Postgres pointer**. Considered:

- **LakeFS** — git-like branching/commits over object storage; powerful but an extra stateful service.
  Kept as a *later* hook: it can sit under the same `dataset_version` pointer if branching becomes a
  need, without changing the app contract.
- **DVC** — git-centric; awkward at 10⁶-row scale.
- **HF datasets revisions** — great *if* data originates on HF; we support **importing** an HF revision
  *into* a snapshot, but don't depend on HF as the system of record.

---

## 13. Model egress — single mandatory gateway for *all* traffic, then *external-only* (settled back)

This one oscillated; recording the path so it isn't re-litigated:

1. **Original:** LiteLLM as the single mandatory egress for **all** traffic, including self-hosted
   vLLM.
2. **Reversed (interim):** force only **external** traffic through the gateway; route self-hosted vLLM
   **direct** (capacity-LB'd) to avoid a Python-proxy throughput ceiling for in-cluster inference.
3. **Current (v1):** **everything — including vLLM — is gateway-fronted again.** At v1 scale the
   proxy is nowhere near its ceiling, and one path / one rate limiter / one cost tally is simpler. The
   **direct vLLM bypass path is now a deferred future item** (see `docs/FUTURE.md`), triggered only when
   a self-hosted model carries load *and* the proxy is the measured bottleneck.

So the current design is "gateway for all"; the vLLM-direct path is future, not current.
