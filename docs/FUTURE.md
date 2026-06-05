# Eval Engine — Deferred Subsystems & Future Improvements

> Companion to `DESIGN.md`. Work we **intend to build later**, each gated on a *measured* trigger,
> kept out of the current-design docs so those describe only what v1 builds. Distinct from
> `docs/ALTERNATIVES.md` (things we evaluated and do **not** intend to build). The governing rule:
> **defer anything whose later addition is purely additive — no v1 schema or data-path reshape —
> until the workload proves the need.**

---

## 1. Deferral triggers (at a glance)

| Deferred thing | §  | Re-introduce when… |
|---|---|---|
| Weighted-fair-share Scheduler | 2 | A real team measurably starves another under shared-capacity contention. |
| Self-hosted vLLM **direct** path | 3 | A self-hosted model carries load **and** gateway proxy throughput is the measured bottleneck. |
| microVM snapshot-restore sandbox pool | 4 | A churn spike shows agentic throughput exceeding the per-sample-pod ceiling (~low-hundreds/s). |
| Langfuse tracing | 5 | A cross-run, call-level observability need appears that `.eval` + ClickHouse can't answer. |
| Superset BI | 6 | Users ask for ad-hoc slices the canned ClickHouse-backed views don't cover. |
| Postgres plugin catalog | 7 | Untrusted / third-party plugins exist (lands with tenancy enforcement). |
| Hash-bucketed claim | 8 | The batch≈1 / sub-second / thousands-of-single-run-claimers regime appears. |
| Human-review UI | 9 | The review workflow is prioritized (schema is already in place). |
| Multi-tenancy enforcement | 9 | Hard team isolation / per-team billing is required. |
| Multi-container sandbox topologies | 9 | An eval needs an attacker/victim network topology (T3-only). |

Every row is **additive** to re-introduce — none reshapes the v1 schema or data path.

---

## 2. Weighted-fair-share Scheduler

**v1 ships:** two-lane (`interactive`/`batch`) admission with a borrowable interactive reserve + a
fixed per-run concurrency cap (`SCHEDULER.md`). That handles head-of-line latency for small iteration
runs without any per-tick allocation.

**Deferred:** a leader-elected control loop doing **max-min weighted fair share** with spill
redistribution to a fixpoint — a `global_slots` capacity derivation (Little's law for external,
`max_num_seqs × replicas` for vLLM), a per-run `slot_budget` enforced at claim time via a `run_slots`
table, and a published KEDA admittable-slot signal (three consumers, one authority).

**Why deferred:** fair-share only does work when **multiple teams with different weights simultaneously
saturate a shared ceiling**. At ~1000 runs/day with peaks ~50 concurrent runs, mostly one team, that
contention is rare-to-absent. It's also the most novel, most-bug-prone custom IP — keeping it off the
critical path until the workload demonstrates the need is the cleanest risk reduction.

**Additive upgrade path:** the two fixed lanes generalize to N weighted classes; the constant
`max_inflight` becomes the computed `slot_budget`; the claim's `headroom` source switches from a
constant to the `run_slots` row. No schema reshape on the hot path.

### Sketch of the deferred mechanism (for when we build it)

- **Leader-elected singleton** (Postgres advisory lock), recomputes **soft** allocation each tick;
  crash just lets budgets go stale for seconds. Governs *efficiency & fairness*, not *safety*.
- **`global_slots`** = total concurrent in-flight sample-slots the model-serving tier can sustain
  (external: `rps_cap × avg_request_seconds`; self-hosted vLLM: `max_num_seqs × replicas`).
- **Max-min weighted fair share:** team share = `weight_team / Σ active weights × global_slots`; split
  within team (FIFO by `queued_at`); cap each run by real demand (`queued + running`); redistribute
  spill to backlogged teams to a fixpoint (work-conserving).
- **Three consumers of one allocation:** admission (cap concurrent running runs), the claim layer
  (per-run `slot_budget`), and KEDA (`admittable_total = Σ slot_budget`, `maxReplicas =
  ceil(global_slots / per_worker_concurrency)`).

---

## 3. Self-hosted vLLM direct (bypass) path

**v1 ships:** vLLM is a **first-class but gateway-fronted** model source — launch against
`vllm/<model>` and it routes through LiteLLM like everything else (one path, one rate limiter, one
canonical cost tally).

**Deferred:** a **second model data path** for in-cluster inference that **bypasses the gateway** —
workers hit vLLM directly behind a **capacity-aware in-path router** (Envoy / vLLM production-stack)
with backpressure (a `queue-full` worker error class alongside `BudgetExceeded`), plus **unified
accounting reconciled from two sources** back into the cost/budget plane.

**Why deferred:** the gateway-as-throughput-ceiling concern is real only at **thousands of calls/s of
in-cluster inference**. At v1 scale (~385 samples/s aggregate, mostly external) LiteLLM fronts vLLM
fine. Building the harder of the two paths for a load that doesn't exist yet is premature.

**Additive upgrade path:** the model-reference indirection (a run names a logical model; the gateway
resolves it) and the provider-agnostic cost interface already exist, so the direct path slots in via a
routing change + a second usage source — no schema reshape. Deferring this also removes the only reason
the deferred Scheduler (§2) would need a vLLM-capacity input.

---

## 4. Pooled microVM snapshot-restore sandbox service

**v1 ships:** agentic evals run on **hardened, air-gapped per-sample pods** via Inspect's K8s sandbox
provider (the baseline hardening + air-gap-by-default is where the security value concentrates and is
cheap). Cold-start is mitigated with pre-pulled images and `expanding`-triggered pre-warm.

**Deferred:** a **pooled sandbox service** that keeps the kube-scheduler/kubelet/IPAM out of the
per-sample path, because pod-per-sample **create/destroy throughput** tops out at low-hundreds/s.
Tier-gated:

- **T1 (benign, narrow tools):** overlay **reset-reuse** (low surface, low stakes).
- **T2 (general untrusted code, the default): microVM snapshot-restore** — each sample runs in a *fresh*
  microVM (Firecracker/Kata via a `RuntimeClass`) restored from a golden snapshot (~tens of ms), then
  discarded. Fresh isolation **and** high churn, with no "prove the reset is airtight" burden (there is
  no reuse). Needs a **KVM-capable node pool**; Kata is the portable fallback where raw Firecracker
  isn't available. Sized by *concurrency*, not arrival×cold-start.
- **T3 (adversarial):** single-use, strongest isolation, dedicated nodes/account; low volume.

**Why deferred:** unbuilt design direction; the prototype validated only the per-sample Docker
*contract* at n=1. If agentic is a minority of the 385/s aggregate (the common early case), per-sample
pods are within budget. **Trigger: a measured churn+restore spike.** This is a top-tier DESIGN risk —
agentic-at-scale is gated on it.

(Warm pools of pre-*booted* single-use pods were considered and dropped — they hide cold-start
*latency* but not create/destroy *throughput*, so they don't move the ceiling. See `ALTERNATIVES.md`.)

---

## 5. Langfuse (per-call tracing)

**Dropped from v1.** Its content — per-call traces, cost, latency — is a reprojection of three things
v1 already treats as load-bearing: the `.eval` log (full transcript per sample), ClickHouse
(`tokens_in/out`, `cost_usd`, `latency_ms` per sample), and the gateway (canonical per-`run_id` cost).

**The one genuine add** is *cross-run, call-level aggregate* observability (e.g. "p99 latency of judge
calls across all runs this week"). **Trigger:** that need appears in practice — and the first attempt is
a **ClickHouse call-records table** (the gateway emits call records to the same store), not a separate
stateful tracing service.

---

## 6. Superset (ad-hoc BI)

**Deferred.** The enumerated analytics (model comparison, pass-rate + CIs, accuracy-by-dimension, cost
dashboards, regression tracking) are a **fixed ~10-query set** → rendered as **canned ClickHouse-backed
views inside the Next.js app** (the prototype already serves accuracy/by-category/cost this way).

**Trigger:** users start asking for **ad-hoc, self-serve slices** the canned views don't cover — at
which point pointing Superset at the same ClickHouse is purely additive. Avoids operating a heavy
stateful BI platform (its own metadata DB, auth, caching) for a capability v1 doesn't need.

---

## 7. Postgres plugin catalog

**v1 ships:** a shared **in-process plugin registry** (entry-point discovery) with JSON Schemas derived
**live** from each plugin's Pydantic config. With a single trusted team, the control plane importing the
same first-party plugin package (same code, same image) is fine.

**Deferred:** syncing plugin metadata into a Postgres `plugins` catalog so the control plane can serve
config schemas **without importing plugin code**, plus the `plugins sync` CI step and a code-ref-match
admission check. This earns its keep only with **untrusted third-party plugins** you refuse to import in
the control plane. **Trigger:** untrusted/third-party plugins exist — it lands together with multi-tenant
plugin isolation.

---

## 8. Hash-bucketed claim

**v1 ships:** the plain ordered claim (`FOR UPDATE SKIP LOCKED`, `ORDER BY sample_id`) bounded by a
per-run `max_inflight`, with `not_before` handling poison-sample head-of-line.

**Deferred:** a `bucket smallint` column + index + `AND bucket = floor(random()*K)` to spread claimers
off the ordered queue head. **Trigger:** a workload shape of batch≈1 + sub-second samples + thousands of
single-run claimers (where claim QPS actually gets high). Purely additive: a column + an index + a
predicate, no migration of existing rows. (Why it's not needed at v1 scale: claim QPS =
`throughput / batch_size` ≈ 8/s at batch 50.)

---

## 9. Product & platform roadmap

Forward-looking phases and capabilities beyond the v1 build:

- **Human-review queue UI.** The schema is already in place (`human` scorer type + `review_status`
  column on sample results). Only the workflow/UI is deferred (D10).
- **Multi-tenancy enforcement.** Ownership columns (`created_by`, `team_id`), roles (admin/member), and
  the audit log exist from day one, so enforcement (hard team isolation, per-team budgets/billing,
  visibility rules) is additive — not a migration.
- **Multi-container sandbox topologies.** Evals needing an attacker+victim host topology with internal
  micro-segmentation, via the K8s provider's multi-pod support: a `topology` concept in the harness
  config (declared pods + allowed internal edges), atomic multi-pod provision/teardown, sweep-by-
  topology. **T3-only** (the outer boundary contains deliberately-successful exploits). v1 supports
  single-pod sandboxes only.
- **GPU-in-sandbox.** If any agentic eval needs a GPU *inside* the sandbox (ML-agent tasks), it changes
  the node-pool and isolation story — open question to revisit then.

### Phased build order (current → future)

- **Phase 0 — Spike (done).** Inspect locally; 1 QA + 1 agentic eval; results → Postgres +
  ClickHouse(flatten). Proved kernel + data model + ETL projection.
- **Phase 1 — Single-node platform.** FastAPI + Postgres + minimal Next.js (launch/list/monitor) +
  embedded Inspect viewer; LiteLLM in front; OIDC login.
- **Phase 2 — Distribution.** KEDA-autoscaled worker Deployment + Postgres ephemeral ledger +
  retries/resume + global rate limiting; scale to large runs.
- **Phase 3 — Analytics.** ClickHouse + canned CH-backed views; model comparison, CIs, regression
  tracking, cost dashboards.
- **Phase 4 — Hardening & scale-out.** Budgets/alerts, audit, retention tiering (flip from keep-all);
  then, as triggers fire: the fair-share Scheduler (§2), the microVM sandbox pool (§4), the vLLM direct
  path (§3), and the human-review queue / tenancy enforcement (§9).

---

## 10. Future open questions

- **Score identity** — is one `primary_score`/`passed` enough, or do some evals need a declared primary
  metric among several (multi-objective)?
- **Heartbeat granularity** — per-sample-step vs wall-clock timer; interaction with Inspect's own
  timeouts.
- **Per-eval network allowlists** — who authors/approves them; default-empty vs templated per tool.
- **T3 separation** — separate node pool within-cluster vs separate cluster vs separate cloud account
  (strength vs ops cost).
- **Worker → vLLM auth/discovery** through the gateway for self-hosted endpoints (secrets handling).
