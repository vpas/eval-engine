# Eval Engine — Complexity Review

> A critical pass focused on **one question only: is each component justified, and is it as
> simple as the §2 scale envelope and the functional requirements allow?** Where a simpler
> design meets the numbers in DESIGN §2 (≈385 samples/s sustained, 1B sample-evals/mo, 12B
> rows retained, 10k runs/mo), I say so and recommend the cut.
>
> **Framing.** The two reviews already in `ISSUES.md` pushed the design *toward* rigor and
> sophistication (a scheduler, a 3-way commit protocol, microVM snapshots, fair-share). That
> work is good and mostly correct. This review applies the **opposite** force: the engine is
> now carrying a lot of machinery sized for problems that the stated scale and the v1 workload
> ("everything is external today", single trusted team) **do not yet have**. Nothing below
> disputes correctness — it disputes *necessity-now*.

---

## 1. Headline: the scale is more modest than the component list implies

The sizing that "all decisions are tuned to" works out to:

| Quantity | Value | What it actually demands |
|---|---|---|
| Sustained throughput | ~385 samples/s | Distribution, yes. Exotic coordination, no. |
| Claim QPS (batch 50) | **~8 claims/s** | A single Postgres laughs at this. |
| Ledger live rows | ≤ a few × 100k (ephemeral) | A small hot table, not a big-data problem. |
| Concurrently *running* runs | realistically single digits–low tens | Fairness is barely contended. |
| CH rows/mo | 1B | Genuinely needs a columnar store. |
| CH retained | 12B | Genuinely needs ClickHouse. |

The two numbers that *do* force real infrastructure are the **12B-row analytics store** and
the **global rate limit across workers**. Almost everything else is sized for a contention
and concurrency regime the workload doesn't reach. The design correctly identified the two
hard problems — and then built first-class subsystems for several soft ones too.

**The single biggest simplification: collapse the control plane from four moving parts to
two, and defer two whole subsystems (the fair-share Scheduler and the self-hosted vLLM path)
that the doc itself admits aren't needed for v1.**

---

## 2. Verdict table

| Component | Verdict | One-line reason |
|---|---|---|
| Inspect AI kernel (used directly) | **Keep** | Correct core bet; the differentiator is the platform, not the kernel. |
| Skinny Postgres ledger + claim/lease | **Keep** | Right-sized after A3; 8 claims/s is trivial. |
| ClickHouse analytics + flatten/ETL | **Keep** | 12B rows genuinely needs it. |
| Object store + zstd + sampling retention | **Keep** | Correct; A12's reversibility argument is sound. |
| Commit ordering (ack-before-flip) | **Keep** | Necessary cost of the skinny ledger. |
| KEDA worker autoscaling | **Keep (simple form)** | But on `count(queued)` + `maxReplicas` cap, per SCHEDULER §8. |
| **Weighted-fair-share Scheduler** | **DEFER (whole subsystem)** | Solves team-contention that ~10–20 concurrent runs don't create. |
| **Scheduler + Reconciler as 2 singletons** | **MERGE → 1** | Same "derive state each tick" loop, same lease. |
| **Self-hosted vLLM direct path (A9)** | **DEFER (whole path)** | "Everything is external today." Don't build the dual data path yet. |
| **Langfuse tracing** | **DROP from v1** | Per-call traces are largely redundant with `.eval` logs + CH. |
| **Superset** | **DEFER** | A handful of canned CH queries in the existing dashboard beat operating Superset. |
| **microVM snapshot-restore sandbox service** | **Keep as direction, don't BUILD for v1** | Per-sample pods are fine until agentic volume proves the churn ceiling. |
| Plugin catalog sync → Postgres | **Simplify for v1** | Shared in-process registry; the PG catalog earns its keep only with untrusted plugins. |
| `dimensions Map` + `category` + `input_hash` | **Keep** | Cheap, additive, avoids an early-binding trap. |
| Content-addressed dataset snapshots | **Keep** | Simplest thing that gives real reproducibility. |
| OIDC + teams + audit columns | **Keep (schema)** | Columns are cheap; don't build *enforcement* (see fair-share). |
| `run_summary` + finalize metrics + live-score-via-FINAL | **Keep, watch** | Acceptable, but it's the most elaborate "simple" thing in the design. |

---

## 3. The cuts, in priority order

### 3.1 Defer the weighted-fair-share Scheduler entirely — biggest win

`docs/SCHEDULER.md` is a whole leader-elected control loop: max-min weighted fair share with
spill redistribution to a fixpoint, a `global_slots` capacity derivation (Little's law for
external, `max_num_seqs × replicas` for vLLM), per-run `slot_budget` enforced at claim time,
and a published KEDA signal — three consumers, one authority.

**Why it's over-built for the scale.** Fair-share only does work when **multiple teams with
different weights are simultaneously saturating a shared capacity ceiling**. At 10k runs/month
(~14/hour) with realistically single-digit-to-low-tens concurrent runs, the contention this
mechanism arbitrates is rare-to-absent. The document *itself* says so:

> "For a single-tenant, QA-only, single-provider deployment … the whole thing collapses to the
> degenerate case … Build order: (1) `maxReplicas` cap + `count(queued)` → (2) … → (3)
> weighted-fair-share." — SCHEDULER §8

So the design already knows steps 2–3 are deferrable. I'd go further: **steps 2 and 3 are a
v3 concern, not v1.** What you actually need on day one:

- **A global admission cap** (max N concurrent running runs) — one integer, enforced at
  `queued→expanding`. This alone bounds blast radius and protects shared capacity.
- **A per-run concurrency cap** (a fixed `max_inflight` per run, or simply `global_slots / N`)
  — prevents one run from eating the whole cluster. A constant, not a recomputed allocation.
- **KEDA on `count(queued)` with a sane `maxReplicas`** — SCHEDULER §8 confirms the cap alone
  prevents over-provisioning.

That's a few constants and one admission check, versus a leader-elected fixpoint solver plus
a `run_slots` table read on every claim. Fairness, when it finally bites, can start as **FIFO
admission** (oldest queued run admitted first) — which needs no per-tick allocation at all.
Add weights only when a real team genuinely starves a real team. **Defer `docs/SCHEDULER.md`
§3–§7 to v3; keep §8 step 1 as the v1 design.**

This is also the cleanest cut because it removes the most novel, most-likely-to-harbor-bugs
custom IP from the critical path for a problem you can't demonstrate you have.

### 3.2 Merge the Scheduler and Reconciler into one orchestrator singleton

ORCHESTRATION §2 defines **two** leader-elected singletons. The doc admits "they may
co-locate; both are lightweight 'derive state each tick' singletons." With the fair-share
allocation deferred (3.1), the Scheduler's residual job is just admission + expansion — which
is the same tick loop, holding the same advisory lock, that the Reconciler already runs for
progress/finalize/budget. **Make it one component: one lease, one loop, one deployment.** Two
singletons is two failover paths and two lease-holders to reason about for no functional gain
at this scale.

Resulting control plane: **(1) FastAPI** (stateless N replicas), **(2) one Orchestrator
singleton** (admit → expand → reconcile → finalize), **(3) Workers** (KEDA Deployment). Down
from the current four-role split (API / Scheduler / Reconciler / Workers).

### 3.3 Defer the entire self-hosted vLLM path (A9)

The "genuine mix of API + self-hosted (vLLM)" workload (§2) drives a surprising amount of
design surface:

- A *second* model data path (direct, capacity-LB'd, bypassing the gateway).
- An in-path capacity-aware inference router (Envoy / vLLM production-stack) with backpressure
  and a future "queue-full" worker error class (SCHEDULER §3).
- A `global_slots` derivation that has to special-case vLLM vs. external.
- "Unified accounting without a unified data path" — usage reporting reconciled back into the
  cost/budget plane from two sources.

And yet, repeatedly in the docs: *"Future-phase; everything is external today"* (A9),
*"all traffic is external today"* (ISSUES second-round #4). **You are designing the harder of
the two paths for a workload that does not exist in v1.** Until there is an actual self-hosted
model:

- Route **everything** through the LiteLLM gateway (one path, one accounting source, one rate
  limiter). LiteLLM already fronts vLLM endpoints fine at moderate scale.
- Delete the dual-path / in-path-router / vLLM-capacity machinery from the v1 design; keep one
  paragraph noting "if self-hosted throughput outgrows the proxy, vLLM moves to a direct
  capacity-LB'd path (see archived A9)."

The A9 concern (a Python proxy as a throughput ceiling for in-cluster inference) is real —
*when you have in-cluster inference at thousands of calls/s*. That's a measured-trigger
optimization, not a v1 architecture commitment. Deferring it also removes the main reason the
deferred Scheduler needed a vLLM-capacity input — the two cuts reinforce each other.

### 3.4 Drop Langfuse from v1 — it's the third copy of the same data

Langfuse is in the stack for "per-call LLM traces, cost, latency." But that information already
lives in **two** places the design treats as load-bearing:

- The **`.eval` log** (the source-of-truth artifact) contains the full transcript — every
  model call, its messages, and token usage — per sample. The design even embeds the Inspect
  viewer specifically to read these.
- **ClickHouse** holds per-sample `tokens_in/out`, `cost_usd`, `latency_ms`.
- The **gateway** is the canonical per-`run_id` cost tally (A5).

So Langfuse is a fourth datastore (self-hosted, to operate and back up) whose primary content
is a reprojection of the `.eval` transcript. The genuine thing it *might* add is **cross-run,
call-level aggregate observability** (e.g. "p99 latency of judge calls across all runs this
week") that neither the per-run `.eval` log nor the per-sample CH row answers directly. That's
a nice-to-have, not an FR. **Cut Langfuse from v1.** If call-level cross-run analytics becomes
a real need, the gateway can emit call records to the *same ClickHouse* (one more table) rather
than standing up a separate tracing system. This removes an entire stateful dependency from
the operational surface.

### 3.5 Defer Superset — you have three UIs for a known set of queries

The dashboard story is **Next.js (manage/launch/monitor) + embedded Inspect viewer
(transcripts) + Superset (analytics over CH)**. Three frontends, one of them (Superset) a
heavy stateful BI platform with its own metadata DB, auth, and caching to operate.

Superset earns its place when users need **ad-hoc, self-serve BI** — pivoting on dimensions
nobody pre-planned. But the analytics the design actually enumerates are a **fixed, known
set**: model comparison, pass-rate with CIs, accuracy-by-dimension, cost dashboards,
regression tracking (FR8, §11 Phase 3). Those are ~10 parameterized ClickHouse queries. The
prototype already serves accuracy / by-category / cost from canned queries in a single
vanilla-JS page with no build step.

**v1 recommendation:** render the fixed analytics as canned CH-backed views inside the
existing Next.js app. Add Superset later **only if** users start asking for slices you didn't
anticipate — at which point pointing Superset at the same ClickHouse is purely additive. This
drops a major operational component for a capability v1 doesn't require.

### 3.6 Don't *build* the microVM sandbox service for v1 (keep it as the direction)

`docs/SANDBOXING.md`'s tiered model (T1 reset-reuse / T2 microVM snapshot-restore / T3
single-use) is well-reasoned, and the pooled service is genuinely needed *if* agentic evals
run at hundreds/s. But two facts cap its v1 priority:

- It is explicitly **"design-direction, unbuilt"**, gated on a "churn+restore spike" (A10),
  and the prototype validated only the per-sample Docker contract at n=1.
- The K8s pod-per-sample ceiling is "low-hundreds/s." If agentic is a *minority* of the 385/s
  aggregate in v1 (the common case early on), **per-sample pods are within budget** and need
  none of the Firecracker / golden-snapshot / KVM-node-pool / restore-pool-sizing machinery.

**Recommendation:** v1 ships agentic on hardened per-sample pods (the baseline hardening in
SANDBOXING §5 is the part that matters and is cheap). The microVM snapshot-restore service is
a Phase-4 build, triggered by a measured churn ceiling — exactly as the risk register frames
it. Keep the doc; don't let "default tier = T2 microVM" imply v1 must build the pool. The
baseline hardening + air-gap-by-default (§2's insight that the sandbox rarely needs egress) is
where the security value concentrates, and it's the cheap part.

### 3.7 Simplify plugin discovery for v1 (skip the Postgres catalog)

PLUGINS §4 has plugins register in an in-process registry **and** sync metadata into a
Postgres `plugins` catalog, so the control plane can serve config schemas "without importing
plugin code." With a **single trusted team** (the stated v1 trust model, PLUGINS §7), the
control plane importing the same first-party plugin package is fine — it's the same code, same
image. The PG catalog + the `plugins sync` CI step + the dual-registry reconciliation earn
their keep precisely when **untrusted third-party plugins** exist and you refuse to import
them in the control plane — a deferred-with-tenancy concern. **v1: share the in-process
registry; derive JSON Schemas live.** Reintroduce the catalog with the multi-tenant plugin
story. (Low-stakes cut, but it's another table + sync step + a code-ref-matching admission
check removed.)

---

## 4. Things that look heavy but are correctly sized (keep)

To be balanced — these survive the necessity test:

- **ClickHouse + the flatten/ETL projection.** 12B rows over 12 months is past Postgres and
  past DuckDB's comfort zone. The columnar store is one of the two genuinely scale-forced
  decisions. The `.eval`-as-artifact + CH-as-projection split is clean, not a re-abstraction.
- **The skinny ledger + claim/lease.** After A3 this is a small hot table doing ~8 claims/s.
  The "build it vs. buy Temporal/SQS" debate landed correctly: it's a single-step fan-out with
  SQL-shaped control-plane queries, not a workflow engine's job. Keep — and keep `pgmq` named
  as the escape hatch.
- **Ack-before-flip commit ordering.** This is necessary complexity created by the (correct)
  skinny-ledger choice. It's three ordered writes with an idempotency key, not a distributed
  transaction. Fine.
- **Global rate limiting via the gateway + Redis** for external providers. This is the *other*
  genuinely scale-forced piece — provider quotas are shared across all workers, so the limit
  must be global. Mandatory.
- **Sampling-by-default retention + tiering (A12).** The reversibility argument is exactly
  right for the named dominant cost. Keep.
- **`dimensions Map` + content-addressed datasets + reproducibility-honesty (A8).** All cheap,
  additive, and they avoid expensive-to-reverse early bindings. Keep.

---

## 5. One internal tension worth a second look (not a cut, a watch)

The live-metrics path is the most elaborate "simple" thing left after the cuts. To avoid both
the MV double-count (A4) and the lost-live-score (2nd-round #2), the design now has: the
Orchestrator, each tick, runs `avg(passed) FROM sample_results FINAL WHERE run_id=X` against
ClickHouse, plus reads the gateway cost tally, plus reads ledger status counts, and writes all
three to the Postgres `runs` row — while `run_summary` is computed once at finalize. That's
**three data sources reconciled per tick per active run, across two stores**, to render a
progress bar and a live accuracy number.

It's defensible and each piece is individually cheap. But it's worth confirming at build time
that a per-tick `FINAL` aggregate across *every* active run stays cheap as concurrent-run count
grows, and that "live accuracy" is a real user need vs. "final accuracy + a progress bar"
(which needs only the ledger counts and no CH query at all). If live *accuracy mid-run* isn't a
hard requirement, dropping it removes the per-tick CH query entirely — a further simplification.

---

## 6. Recommended v1 architecture (after the cuts)

```
Dashboard:   Next.js (manage/launch/monitor + canned CH analytics views)
             + embedded Inspect viewer (transcripts)
             [no Superset, no Langfuse]
Control:     FastAPI (N replicas, stateless)
             + ONE Orchestrator singleton (admit → expand → reconcile → finalize;
               admission cap + fixed per-run concurrency cap; NO fair-share)
Execution:   Workers (K8s Deployment, KEDA on count(queued) + maxReplicas cap)
             → Inspect Task per shard → results async-insert (ack) → CH; transcript → S3
Model I/O:   LiteLLM gateway for ALL traffic (Redis global rate limit, cost cap)
             [no separate vLLM direct path yet]
State:       Postgres (metadata + skinny ephemeral ledger + advisory-lock leader election)
             ClickHouse (12B-row projection; run_summary at finalize)
             S3/MinIO (.eval logs + sampled transcripts, zstd)
Sandbox:     hardened per-sample pods, air-gapped by default (NO microVM pool yet)
Plugins:     shared in-process registry (NO Postgres catalog yet)
```

**Net effect:** removes two stateful services (Langfuse, Superset), one whole control
subsystem (fair-share Scheduler), one parallel data path (vLLM direct), one singleton role
(merge Scheduler/Reconciler), one sync step + table (plugin catalog), and keeps the microVM
pool on the shelf — **without touching the two decisions that the scale actually forces
(ClickHouse, global rate limiting) or any functional requirement.** Each cut has a named,
measured re-introduction trigger, so nothing is lost, only deferred until the workload
demonstrates the need.

---

## 7. The deferral triggers (so nothing is silently dropped)

| Deferred thing | Re-introduce when… |
|---|---|
| Weighted-fair-share Scheduler (§3–§7) | A real team measurably starves another under shared-capacity contention. |
| Self-hosted vLLM direct path (A9) | An actual self-hosted model exists *and* gateway proxy throughput is the measured bottleneck. |
| Langfuse | A call-level cross-run observability need appears that `.eval` + CH can't answer (first try: a CH call-records table). |
| Superset | Users ask for ad-hoc slices the canned views don't cover. |
| microVM snapshot-restore pool | A churn spike shows agentic throughput exceeding the per-sample-pod ceiling. |
| Postgres plugin catalog | Untrusted/third-party plugins exist (lands with tenancy enforcement). |
| Hash-bucketed claim (A6) | The batch≈1 / sub-second / thousands-of-claimers regime appears (already correctly deferred). |

Every one of these is **purely additive** to re-introduce — none requires reshaping the v1
schema or data path. That's the test the design should hold itself to: *defer anything whose
later addition is additive, until the workload proves the need.* The architecture already
applies this test well to A6; this review just applies it consistently to the rest.

---

# Response to the complexity review (decisions made)

We accept the review essentially in full — it enforces the design's *own* deferral discipline
(SCHEDULER §8 build-order, the "future-phase" tags on A9, the "unbuilt, trigger-gated" framing on
A10) consistently across the stack. All seven cuts are adopted; the one genuine open decision (§5)
is resolved. Decisions are now authoritative in **DESIGN §16** (new), with banners + inline edits
threaded through the companion docs. Each cut is a *pure deferral* with a measured trigger — no v1
schema or data-path reshape is required to re-introduce any of them.

**Requirement update incorporated:** runs/day revised to **~1000** (a mix of full runs and small
**subset runs for fast iteration**). We re-derived concurrency (Little's law): low-tens, peaks ~50
concurrent running runs — still far below where a weighted fixpoint allocator earns its keep, so the
verdicts hold. The subset-run workload *did* surface one new concern (head-of-line latency for quick
iteration runs), addressed below without reinstating the Scheduler.

### C1 — Fair-share Scheduler → DEFER to v3. **Adopted, with a v1 refinement.**
v1 = **two-lane admission** (`interactive` / `batch`) with a **reserved interactive slice** so a
quick subset run never queues behind big batch runs, a per-lane admission cap, and a **fixed per-run
concurrency cap** (the load-bearing fairness primitive — a big run can't eat the cluster). This is
constants + an admission comparator; **no `run_slots` table on the claim path, no per-tick
allocator.** The two fixed lanes generalize additively to N weighted classes when fair-share returns.
SCHEDULER.md §3–§7 are now marked v3; a new **SCHEDULER §0** specifies the v1 lane model.
*Trigger:* a real team measurably starves another under shared-capacity contention.

### C2 — Merge Scheduler + Reconciler → one Orchestrator singleton. **Adopted.**
With fair-share deferred, the residual job (admission + expansion) is the same "derive state each
tick, one advisory lock" loop the Reconciler runs. v1 control plane = **FastAPI (N) + one
Orchestrator singleton + Workers**. Admission and reconcile stay distinct *functions* so a future
re-split is a refactor. (ORCH §2.)

### C3 — Self-hosted vLLM direct path → DEFER. **Adopted, reframed (requirement: vLLM matters).**
vLLM stays **first-class** in v1 — you launch a run against `vllm/<model>` and it works — it just
rides the **gateway path** like everything else (one path, one rate limiter, one canonical cost
tally). Only A9's **direct-bypass apparatus** (capacity router, backpressure, "queue-full" error
class, dual-source accounting) defers. Consequence: `global_slots` (when fair-share returns) derives
purely from the gateway rate cap — no vLLM-capacity special-case in v1. *Trigger:* a self-hosted
model carries load **and** gateway proxy throughput is the measured bottleneck.

### C4 — Langfuse → DROP from v1. **Adopted.**
Its content is a reprojection of the `.eval` log + ClickHouse (`tokens/cost/latency`) + the gateway
cost tally. *Trigger:* a cross-run, call-level observability need `.eval` + CH can't answer — first
attempt a CH call-records table, not a new service.

### C5 — Superset → DEFER. **Adopted.**
The enumerated analytics are a fixed ~10-query set → canned CH-backed views in the existing Next.js
app (the prototype already serves accuracy/by-category/cost this way). *Trigger:* users ask for
ad-hoc slices the canned views don't cover (point Superset at the same CH — additive).

### C6 — microVM sandbox pool → don't BUILD for v1. **Adopted (doc-framing fix).**
v1 ships agentic on **hardened, air-gapped per-sample pods** (the cheap, high-value part). The pooled
microVM snapshot-restore service stays the documented direction; "default tier = T2" describes the
target, not a v1 build obligation. *Trigger:* a measured churn spike past the per-sample-pod ceiling.

### C7 — Postgres plugin catalog → DEFER. **Adopted.**
Single trusted team → control plane shares the **in-process registry** and derives JSON Schemas live.
*Trigger:* untrusted/third-party plugins exist (lands with tenancy enforcement).

### §5 — Live metrics. **Decision: keep live accuracy, made cheap.**
We keep live mid-run accuracy for **all** runs (it gives early-kill signal on long batch runs at
1000/day) but make the read cheap instead of dropping it: **drop `FINAL` from the live read** and
**throttle it to ~once/minute per run** (progress + cost stay per-tick from cheap sources). Finalize
keeps `FINAL` in `run_summary` — exact where it matters, approximate where it's a live gauge. *This
consciously reverses the previous round's "final comment #2"* (which added `FINAL` to the live read
"at ~zero cost"): that held at single-run scale, not at tens-concurrent; the tiny live skew is the
accepted trade. (DESIGN §16.2; ORCH §7; SCHEMA §2.)

### Kept (survive the necessity test, per review §4): unchanged.
ClickHouse + flatten/ETL; skinny ledger + claim/lease (+ `pgmq` escape hatch); ack-before-flip commit
ordering; gateway + Redis global rate limiting; sampling-by-default retention + tiering (A12);
`dimensions Map` + content-addressed datasets + reproducibility-honesty (A8). None touched.

**Net effect:** removes two stateful services (Langfuse, Superset), one control subsystem
(fair-share), one singleton role (merge), one table + sync step (plugin catalog); defers one build
(microVM pool) — without touching the two scale-forced decisions (ClickHouse, global rate limiting)
or any functional requirement. The v1 target architecture is in **DESIGN §16.3**.

---

# Reviewer assessment of the response

Verdict: a strong, well-reasoned response — accepted in the right spirit, with two refinements
that are actually *better* than what the review proposed, and one earlier decision consciously
reversed with a sound, scale-conditional justification. The response doesn't rubber-stamp:

- **C3 (vLLM) reframe is better than the review's "defer the whole path."** Keeping vLLM
  *first-class via the gateway path* and deferring only the direct-bypass apparatus preserves the
  capability while shedding the complexity. That's the correct seam — the review over-rotated toward
  "defer the path"; "defer the *bypass*" is sharper.
- **C1's two-lane (interactive/batch) refinement** is the right answer to the new subset-run
  requirement — it solves head-of-line latency with an admission comparator + a reserved slice
  instead of reinstating the allocator. Good restraint; the two fixed lanes generalize additively
  to N weighted classes later.
- **§5 reversal is intellectually honest.** Explicitly flagging "this reverses the previous round's
  #2, which held at single-run scale but not at tens-concurrent" is exactly the scale-conditional
  reasoning the whole review asked for.

## Three things still worth flagging (none reopens a cut)

**1. Must-fix (concrete): the run-scoped CH reads won't use the sort key — which undercuts the §5
"made cheap" claim.** `sample_results` is `ORDER BY (eval_id, target_id, run_id, sample_id)`. Both
the throttled live read and the finalize `run_summary` read filter on **`run_id` alone** (confirmed
in the prototype: `analytics_ch.py` → `WHERE run_id=%(r)s`). `run_id` is the *third* key column, so
the predicate can't binary-search the primary index — it falls back to scanning the whole current-
month partition (up to ~1B rows), once/min × ~50 concurrent runs. Dropping `FINAL` cuts merge work
but **not** this scan. Fix is cheap and the data is on hand: the Orchestrator holds the run record,
so pass the full prefix — `WHERE eval_id=… AND target_id=… AND run_id=…` — on both the live and
finalize reads, hitting the sort-key prefix. Should be written into ORCH §7 / SCHEMA §2 explicitly,
because "live accuracy made cheap" is only true after this change.

**2. A core scale number changed mid-response and should be re-pinned, not buried.** Runs went from
DESIGN §2's *10k/month* to *~1000/day* (≈30k/month, 3×). The verdicts hold (peak ~50 concurrent is
still nowhere near fixpoint-allocator territory), but two derived figures need restating so the doc
stays self-consistent: (a) confirm **sample-evals/month is still ~1B** — it sizes ClickHouse *and*
the ~8 claims/s figure that justifies "no sharded claim"; if subset runs are *additive*, samples
rise. (b) 3× the run count triples expansion/finalize lifecycle ops — still small, but DESIGN §2
should carry the new number rather than have it live only in this response.

**3. Make the interactive reservation borrowable (one line).** A *hard* reserved interactive slice
idles capacity whenever no interactive run exists — at 1000/day that's a real duty-cycle waste.
Letting batch borrow the reserved slice when the interactive lane is empty (yield on interactive
arrival) keeps it work-conserving. Caution: keep "borrow + yield" at the *admission* boundary, not
mid-run preemption — otherwise the allocator creeps back in through the side door.

## Net
C1–C7 are all correctly adopted and the kept list is right; nothing here reopens a cut. #1 is the
only must-fix (it's a correctness-of-the-cost-claim issue at 12B rows); #2 and #3 are
consistency/efficiency tidies. The response landed the review well.

---

# Response to the reviewer assessment (3 follow-ups — all applied)

All three landed; #1 is a real correctness-of-the-cost-claim fix, #2/#3 are tidies. None reopens a cut.

**#1 — run-scoped CH reads must use the full sort-key prefix. FIXED.** Correct catch: `run_id` is
the 3rd `ORDER BY` column, so `WHERE run_id` alone can't use the primary index → full current-month
partition scan, which is exactly the cost the §5 "made cheap" claim was trying to avoid (dropping
`FINAL` doesn't touch it). A run is one eval × one model, so the Orchestrator already holds
`eval_id`+`target_id` — both the throttled **live read** and the finalize **`run_summary` read** now
filter `WHERE eval_id=E AND target_id=T AND run_id=X`, hitting a tight index range. Written explicitly
into **SCHEMA §2** (with a code comment on the finalize query explaining why), **ORCH §7**, and
**DESIGN §16.2 / §15 A4**.

**#2 — re-pin the scale number. FIXED.** **DESIGN §2** now carries **~1000 runs/day** (≈30k/month, 3×
the old estimate) with a **run-count reconciliation** note: the 3× is a workload *mix* (many small
subset/iteration runs), not 3× the samples — a representative mix (≈300 full × ~100k + ~700 subset ×
~5k per day ≈ 33.5M/day) lands at **~1B sample-evals/month**, so both scale-forced figures hold
(ClickHouse ~1B/mo·~12B retained; ~8 claims/s → no sharded claim). Explicitly flagged that *if* subset
runs become additive on top of the full-run budget, the only thing to revisit is ClickHouse sizing —
nothing in the control plane.

**#3 — make the interactive reservation borrowable. FIXED (promoted to v1).** The hard reserve would
idle capacity whenever no interactive run exists. **SCHEDULER §0** now makes the reserve **borrowable
in v1**: batch is admitted into the reserve when the interactive lane is empty and **yields by
attrition** on interactive arrival (stop admitting batch into the reserve; never kill running
samples). The reviewer's guardrail is written in as the load-bearing constraint: **borrow + yield
live entirely at the *admission* boundary, never mid-run preemption** — that's the line that keeps the
deferred fixpoint allocator from creeping back in.
