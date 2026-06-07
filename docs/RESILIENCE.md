# Resilience & HA — single points of failure + the plan to fix them

A walk over the running topology (`deploy/k8s/`, `deploy/terraform/`, the worker/orchestrator/control
code) looking for **single points of failure (SPOF)** — anything whose loss stalls or stops the
platform — plus broader availability improvements. Findings are grounded in the current code/manifests;
each carries a concrete fix and a cost/scope note. The actionable backlog is at the end (§9).

> Scope note: the project deliberately runs **cost-minimal** (one zonal cluster, free control plane, a
> single always-on system node unless `ha_stateful=true`, spot workers — `deploy/terraform/main.tf`).
> Several SPOFs below are *intentional* test-grade trade-offs. This doc separates "cheap fixes worth
> doing now" from "real HA that costs money" so we can choose deliberately rather than by accident.

> **POC decision (2026-06-07): single-zone is accepted.** For the proof-of-concept we keep the cluster
> and all node pools in **one GCP zone** — a zonal outage taking down the whole platform is an
> acknowledged, accepted risk (it trades zone-redundancy for the free zonal control plane and ~⅓ the
> always-on node cost). So finding #1 below is **not** a gap to fix now; it's documented as the known
> ceiling on availability and the trigger/path to lift it later. Everything else in this doc — the
> control/edge singletons and connection robustness — is in scope, because those fail *within* a single
> healthy zone (node drain, pod crash, Neon blip) and the cheap fixes are what make the POC survive a
> normal day. Net: zone-level HA is out; **node-/pod-level HA within the zone is in.**

---

## 1. The dependency graph (what depends on what)

```
                      ┌──────────── ingress-nginx ─────────────┐
   browser ──TLS──▶   │   oauth2-proxy (auth gate, replicas:1)  │
                      └───────┬─────────────────┬───────────────┘
                       /grafana            everything else
                              │                 │
                          grafana        frontend (replicas:1)
                                                 │  /api/*
                                          eval-engine-api (replicas:1)
                                                 │
   orchestrator (replicas:1) ───────────┐        │
   worker (KEDA 0..3, SPOT) ────────────┼────────┤
                                        ▼        ▼
                              ┌─────────────────────────────┐
                              │  POSTGRES (Neon, external)   │ ◀── KEDA scaler also polls this
                              │  ledger + runs + leader lock │
                              │  + heartbeats + registry     │
                              └─────────────────────────────┘
                                        ▲        ▲
   worker commit (ack-before-flip) ─────┘        │
                                                 │
                              ClickHouse (HA: 3-pod StatefulSet, system pool)
                              Redis (HA: 3-pod + Sentinel, system pool)
                              LiteLLM gateway (replicas:2) ──▶ OpenRouter
                              GCS bucket (transcripts + .eval logs)
```

**Everything funnels through Postgres.** It is the ledger (claim/lease/commit), the run metadata, the
leader-election lock, the KEDA scale signal, the heartbeat table, and the entity registry. It is the
one component whose loss takes down *all four* roles (API, orchestrator, worker, autoscaler) at once.

---

## 2. SPOF inventory

| # | Component | Current state | Blast radius if lost | Severity |
|---|---|---|---|---|
| 1 | **GCP zone** | zonal cluster + zonal node pools (`main.tf` `location = var.zone`) | Total outage — control plane, system pool, all data | ⚪ Accepted (POC) |
| 2 | **Postgres / Neon** | single external endpoint; no client retry/reconnect | Total stall — no claim/commit/admit/scale | 🔴 Critical |
| 3 | **oauth2-proxy** | `replicas: 1` (`61-oauth2-proxy.yaml`) | Entire UI **and** API unreachable (it's the front gate) | 🔴 Critical |
| 4 | **DB connection handling** | thread-local conn cached, only `con.closed` checked; no retry | Any transient blip / Neon failover crashes the loop | 🟠 High |
| 5 | **eval-engine-api** | `replicas: 1`, no PDB (`40-control-plane.yaml`) | Launch + status/results gap on node drain/crash | 🟠 High |
| 6 | **orchestrator** | `replicas: 1` though code is leader-elected | No hot standby; admit+finalize gap until reschedule | 🟠 High |
| 7 | **ClickHouse on the commit hot path** | ack-before-flip inserts to CH *before* flipping ledger | CH outage stalls **all** worker progress, not just reads | 🟠 High |
| 8 | **frontend** | `replicas: 1` (`80-frontend.yaml`) | Dashboard down (API still reachable directly) | 🟡 Medium |
| 9 | **KEDA operator** | single-replica default Helm install | Workers freeze at current count (no scale up/down) | 🟡 Medium |
| 10 | **LiteLLM `allowed_fails: 0`** | one bad upstream marks the deployment down | Cascade → every model call 429s → runs stall | 🟡 Medium |
| 11 | **Worker pool is spot-only** | no on-demand fallback (`main.tf` workers pool) | Mass spot eviction → throughput 0 until capacity returns | 🟡 Medium |
| 12 | **No PodDisruptionBudgets** | none defined anywhere | Node upgrade/drain can evict the sole replica of a singleton | 🟡 Medium |
| 13 | **Single-pod stateful (non-HA path)** | `10-clickhouse.yaml`/`11-redis.yaml`, `Recreate`, RWO PVC | If those variants are applied: node loss strands the PVC | 🟢 Low |
| 14 | **GCS bucket** | one regional bucket | Transcript/`.eval` read+write loss (GCS itself is HA) | 🟢 Low |

---

## 3. Top findings (detail)

> §3.1 is the **accepted POC ceiling** (single-zone). §3.2–§3.4 are the critical/high in-scope items.

### 3.1 — Zonal cluster — ACCEPTED for the POC (the known availability ceiling)
`deploy/terraform/main.tf`: `location = var.zone` for the cluster and **every** node pool. The GKE
control plane is single-zone (the free tier) and all system-pool pods (ClickHouse, Redis, LiteLLM,
API, orchestrator, oauth2-proxy, frontend) live in that one zone. The ClickHouse/Redis HA StatefulSets
spread across *nodes* via `podAntiAffinity` (`10-clickhouse-ha.yaml`, `11-redis-ha.yaml`) — but all
those nodes are in the same zone, so that "HA" survives a *node* loss, not a *zone* loss. A zonal
outage is a full outage.

**Decision: accepted for the POC, not a fix-now item.** Single-zone is a deliberate cost trade-off —
the zonal control plane is free and the always-on footprint is ~⅓ the regional cost. We accept that a
zonal outage is a total outage; that is the explicit **ceiling on availability** for the POC. The rest
of this doc deliberately targets failures that happen *within* a healthy zone (node drain/upgrade, pod
crash, a Neon failover/cold-start) — those are frequent and cheap to survive, and are what the Phase-1
fixes address.

**Path to lift it later (FUTURE-class, when the POC graduates):** go regional —
`google_container_cluster.location = var.region`, regional node pools (node_count is *per zone*), and
`topology.kubernetes.io/zone` `topologySpreadConstraints` on the stateful sets. This roughly **3×'s**
the always-on node cost and the regional control plane is no longer free. Implement as a
`regional_ha=true` Terraform toggle (mirroring the existing `ha_stateful`) so it stays a *named,
triggered* decision rather than a silent default. **Trigger:** the platform moves past POC to anything
with an availability SLO. Until then: no action.

### 3.2 — Postgres is the universal coordinator
Neon (managed, external — the `eval-pg` secret) gives us storage durability and its own failover. But:

- **It is the one shared dependency of all four roles.** A Neon maintenance window, failover, or
  connection-limit exhaustion stalls claim, commit, admit, finalize, *and* KEDA scaling simultaneously.
- **Neon autosuspend.** On the free/scale-to-zero tier the compute suspends when idle; the first query
  after idle pays a cold-start (seconds) and can time out. With no retry (§3.3) that surfaces as a
  crash-loop on a freshly-woken cluster.
- **Connection ceiling.** ~~Every worker *thread* opens its own thread-local connection~~ — *now bounded:*
  the storage tier shares one `psycopg_pool.ConnectionPool` per process (`EVAL_ENGINE_PG_POOL_MAX`,
  default 10), so the worker, its lease-renew daemon, and the API draw from a capped set rather than one
  backend per thread. The orchestrator's dedicated leader connection still sits outside the pool (a
  session-scoped advisory lock must own a stable connection). `control.pg_connections()` remains the
  gauge; per-process usage is now capped, so fan-out can no longer exhaust Neon's limit by thread count.

**Fix:**
- Point `EVAL_ENGINE_PG_DSN` at Neon's **pooled (PgBouncer) endpoint**, not the direct one — absorbs
  connection churn and the thread-per-connection model. (Note: the leader advisory lock is *session*
  scoped; keep the leader connection on a **direct/session-mode** endpoint so the lock isn't silently
  dropped by transaction-pooling — `control.release_leader` already documents the pgbouncer caveat.)
- Add the connection-resilience wrapper (§3.3) so a Neon failover/cold-start is retried, not fatal.
- Surface `pg_connections()` on the ops dashboard with an alert threshold below Neon's limit.

### 3.3 — No connection retry / health-check (the highest-value cheap fix)
`control._conn()` caches one autocommit connection per thread and only re-opens when `con.closed` is
true. A connection dropped *underneath* us (Neon failover, idle reaper, NAT timeout, network blip) is
**not** `closed` — psycopg raises `OperationalError`/`InterfaceError` on the next `execute`. Nothing in
`control.py`, `analytics.py`, or the worker/orchestrator tick loops wraps queries in retry, so:

- a worker's `claim_batch`/`commit_result` raises → the `_drain_run`/`main` loop has no `try/except`
  → the process dies → k8s restarts it (self-healing, but noisy, and loses the in-flight batch's
  progress to lease-reclaim).
- the orchestrator's `tick()` raises → same crash → re-contends for leadership (≈ up to the 20s reap
  window + 5s standby poll of downtime per §3.6).
- ClickHouse's `analytics._client` is a module global that is **never** re-created on failure — once it
  goes bad it stays bad until the process restarts.

**Fix:** a small `with_retry`/auto-reconnect helper in the storage tier:
- On `psycopg.OperationalError`/`InterfaceError`: drop the cached `_local.con` (and `analytics._client`),
  reconnect, retry with bounded exponential backoff (e.g. 3 tries, 0.2→2s).
- A cheap liveness ping (`SELECT 1`) when reusing a connection that's been idle beyond a threshold, so a
  silently-dead socket is replaced *before* the real query.
- Keep it transparent — wrap `_conn().execute` at the helper level so call sites don't change.

This is **low-risk, in-scope, high-value** — it turns transient infra hiccups (Neon failover/cold-start,
brief CH unavailability) from crash-loops into silent recoveries. **Do this first.**

### 3.4 — oauth2-proxy is a single-replica edge gate
`61-oauth2-proxy.yaml` `replicas: 1`. It sits in front of *both* the frontend and (via path routing)
Grafana, and injects `X-Auth-Request-Email` that the API trusts. If it dies, the entire surface is
unreachable until reschedule — and on a node drain with no PDB it can be evicted with nothing to take
over. **Fix:** `replicas: 2` + a PDB (`minAvailable: 1`). It's stateless (cookie-based sessions); cheap.

---

## 4. High-severity findings (detail)

### 4.1 — API single replica, no PDB
`eval-engine-api` `replicas: 1`. Stateless, so the only cost of HA is one more small pod. A node
upgrade/drain or crash creates a control-plane gap (no launches, no status/results) until reschedule.
**Fix:** `replicas: 2` + `topologySpreadConstraints` (once multi-node) + PDB `minAvailable: 1`.

### 4.2 — Orchestrator runs 1 replica but the code already supports HA
This is a **latent, free win.** `orchestrator.main()` is fully leader-elected: a Postgres advisory lock
(`acquire_leader`), a standby loop, graceful handover on SIGTERM (`release_leader`), and a stale-leader
reaper for ungraceful death (`reap_stale_leader`). The manifest comment ("single orchestrator;
leader-election is a later refinement") is **stale** — it's implemented and tested. But
`40-control-plane.yaml` still sets `replicas: 1`, so there is no warm standby: on node loss we wait for
a full pod reschedule (minutes) instead of a ~1s handover.
**Fix:** bump `eval-engine-orch` to `replicas: 2` and add a PDB. The second pod sits as a standby
(it heartbeats `leader:false`) and takes over near-instantly. Near-zero extra cost (tiny pod, idle).

> **Gotcha found while rolling this out (2026-06-07):** at 2 replicas BOTH pods logged `up, leader` —
> split-brain. Root cause: `EVAL_ENGINE_PG_DSN` points at Neon's **pooled (`-pooler`) endpoint**, and a
> TRANSACTION pooler doesn't preserve the backend session that `pg_try_advisory_lock` (session-scoped)
> relies on, so every replica "acquires" the lock. The leader-election code was correct; the pooled DSN
> silently defeated it (never exercised at `replicas: 1`). **Fix shipped:** the dedicated leader
> connection now uses a SESSION-mode endpoint via `control._leader_dsn()` — an explicit
> `EVAL_ENGINE_PG_LEADER_DSN`, else Neon's direct host derived by dropping `-pooler`. Only that one
> connection changes; the rest of the app keeps the pooled endpoint. This is the orchestrator-lock slice
> of item D, promoted from deferred because at `replicas: 2` it's a correctness requirement, not a
> tuning nicety.
>
> **One-time cleanup after the fix:** the split-brain era left an *orphaned* advisory lock — a PgBouncer
> server backend (`application_name='pgbouncer'`) still holding `pg_try_advisory_lock(LEADER_KEY)`. The
> auto-reaper (`reap_stale_leader`, needs `state='idle'` for >20s) never cleared it because pgbouncer
> keeps reusing that backend, resetting `state_change`. Symptom: every orchestrator pod stuck logging
> `standby — another holder has leadership` with no live leader. Clear it once by terminating the holder
> via the **direct** endpoint:
> `SELECT pg_terminate_backend(a.pid) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid WHERE l.locktype='advisory' AND l.granted AND l.classid=0 AND l.objid = (LEADER_KEY & x'FFFFFFFF'::bigint);`
> No recurrence after the fix — the leader lock now only ever travels the session-mode endpoint.

### 4.3 — ClickHouse sits on the worker's critical write path
The ack-before-flip commit (`runner._commit_batch`) does `analytics.insert(...)` **before**
`control.commit_result(...)` flips the ledger row to `done`. The invariant ("`done` ⟹ durable in
ClickHouse") is correct and valuable — but it means **a ClickHouse outage stalls all worker progress**,
not just the analytics/results read path. Combined with no insert retry (§3.3), a CH blip currently
crashes the worker mid-batch. The HA ClickHouse (`10-clickhouse-ha.yaml`, 3 replicas + embedded Keeper
quorum) mitigates *node* loss, but the coupling remains: if the CH service is unreachable, execution
halts cluster-wide.

**Fixes (pick one; they stack):**
1. **Cheap:** apply the retry wrapper (§3.3) to `analytics.insert` so a brief CH unavailability is
   ridden out rather than crashing the batch. The ledger row stays `running`, gets re-claimed, and the
   higher-`attempt` re-insert wins (ReplacingMergeTree) — correctness already holds.
2. **Structural (decouple):** flip the ledger to `done` on the *Postgres* commit and treat the
   ClickHouse load as an **async projection** driven by the existing `loaded` flag + the
   `ix_tasks_load` partial index + the orchestrator's `_batch_load` safety sweep. The ledger (Postgres)
   becomes the sole source of truth on the hot path; ClickHouse can lag or be briefly down without
   stalling execution. Trade-off: weakens the exactly-once "done⟹durable-in-CH" guarantee to
   "done⟹durable-in-PG, eventually-in-CH" — acceptable since CH is explicitly a *projection*, and the
   `loaded` machinery is already half-built for exactly this. Document as a DESIGN decision before
   doing it. Recommend (1) now, (2) as a considered follow-up.

---

## 5. Medium-severity findings (detail)

- **Frontend `replicas: 1`** (`80-frontend.yaml`). UI-only blast radius (the API is independently
  reachable in-cluster). **Fix:** `replicas: 2` + PDB. Cheap.
- **KEDA operator** is a single-replica default install (`helm.tf`). If it's down, the worker
  Deployment freezes at its current replica count — in-flight work continues (workers are
  self-coordinating via the ledger), but no scale-up on a new burst and no scale-to-zero when idle
  (cost). It also adds a *second* Postgres consumer (it polls the ledger directly). **Fix:** run the
  KEDA operator with `--set operator.replicaCount=2` (it leader-elects internally); accept the PG
  dependency or note it.
- **LiteLLM `allowed_fails: 0` + `num_retries: 0`** (`30-litellm.yaml`). Deliberately fail-fast, but as
  the in-file comment records, a single bad upstream/handler once marked the whole model deployment
  down and cascaded into stuck runs (hence the pinned image digest). The gateway has `replicas: 2`
  (good), but the brittle config is itself a reliability risk. **Fix:** consider a small `allowed_fails`
  with a short cooldown so one transient upstream 5xx doesn't down the deployment; keep the digest pin;
  add a gateway health alert. Workers already have `EVAL_ENGINE_MODEL_TIMEOUT=120` so a hung call can't
  wedge a worker — good, keep it.
- **Worker pool is spot-only** (`main.tf` workers pool `spot = true`, no on-demand fallback). Spot
  eviction is correctly a non-event for *correctness* (lease-reclaim), but a regional spot capacity
  crunch can evict *all* workers at once → throughput drops to zero until capacity returns. **Fix
  (optional):** a small on-demand fallback pool (or GKE spot+on-demand mix) for a guaranteed floor of
  worker capacity. Cost trade-off; document as a toggle.
- **No PodDisruptionBudgets anywhere.** Voluntary disruptions (node upgrade, autoscaler downscale,
  `kubectl drain`) can take the sole replica of any singleton with nothing to stop them. **Fix:** add
  PDBs (`minAvailable: 1`) for api, orchestrator, frontend, oauth2-proxy, litellm, and the
  ClickHouse/Redis StatefulSets once they're multi-replica. Cheap, high value — pairs with the
  replica bumps above.

---

## 6. Low-severity / acceptable-as-is

- **GCS bucket** (`main.tf`): one regional bucket for transcripts + `.eval` logs. GCS is itself
  highly available and regionally redundant; a single bucket is fine. Transcript writes are sampled and
  off the correctness path (the ledger/analytics carry the scored result). Leave as-is.
- **Single-pod ClickHouse/Redis variants** (`10-clickhouse.yaml`, `11-redis.yaml`): only applied on the
  cost-minimal single-node path; `manifests.txt` ships the **-ha** variants by default. No action beyond
  keeping the default on the HA manifests for any environment that matters.
- **Schema-init Job** (`20-schema-init-job.yaml`): one-time bootstrap, not a runtime SPOF. `db.init()`
  is idempotent and every role re-ensures schema at startup, so a missed Job self-heals.

---

## 7. Existing strengths (don't regress these)

The execution layer is already resilient in the ways that matter most for *correctness*:

- **Lease-based crash safety** — a dead worker's claimed tasks are reclaimed once the lease lapses
  (`claim_batch` reclaims `running` rows past `lease_expires_at`); long batches renew via a background
  heartbeat (`worker._execute_with_heartbeat` + `control.renew_lease`).
- **Retry-with-backoff** on transient sample failures (`control.retry_or_fail`, exp `not_before`),
  poison samples back off instead of head-of-line blocking.
- **Ack-before-flip exactly-once** — `done` rows are guaranteed durable in analytics; a crash between
  insert and flip re-runs harmlessly (higher-`attempt` ReplacingMergeTree version wins).
- **Idempotent finalize** — keyed on the run's authoritative `total`; a crash mid-finalize just re-runs
  the no-op steps.
- **Graceful drain** on SIGTERM (workers finish the in-flight batch; `terminationGracePeriodSeconds:
  150` > sample timeout) and **graceful leader handover** (orchestrator releases the advisory lock).
- **Budget + timeout caps** — per-request `MODEL_TIMEOUT`, per-sample `SAMPLE_TIME_LIMIT`, and a
  budget sweep so a runaway run can't burn unbounded cost/time.
- **HA stores available** — ClickHouse (3× + Keeper quorum) and Redis (3× + Sentinel failover) HA
  variants exist and are the default in `manifests.txt`.

The gaps are concentrated in the **control/edge tier** (singleton pods) and **connection robustness**,
not the data-correctness layer.

### 7.1 — Recovery-time note (orchestrator)
On an *ungraceful* leader death (SIGKILL/OOM/node loss, SIGTERM never runs), the standby waits out the
stale-lock reaper: `STALE_LEADER_SECONDS=20` + the 5s standby poll ≈ **up to ~25s** of no admit/finalize
ticks. In-flight workers keep going (they don't need the orchestrator), so this is a brief scheduling
pause, not an outage. A warm standby (§4.2) doesn't shorten the reap window but removes the
pod-reschedule delay on top of it. Acceptable; tune `STALE_LEADER_SECONDS` down only if needed.

---

## 8. Quick wins vs. real-HA (decision summary)

> Decision column reflects the §8a decision log (2026-06-07).

| Lever | Effort | Cost | Decision |
|---|---|---|---|
| Connection retry/reconnect wrapper (§3.3) | Small (code) | $0 | ✅ **Do now** — highest value/effort |
| Retry `analytics.insert` (CH hot-path) (§4.3.1) | Small | $0 | ✅ **Do now** (part of the wrapper) |
| Orchestrator `replicas: 2` (code already HA) (§4.2) | Trivial | ~$0 | ✅ **Do now** |
| API / frontend / oauth2-proxy `replicas: 2` (§3.4, 4.1, 5) | Trivial | small | ✅ **Do now** |
| PodDisruptionBudgets for all singletons (§5) | Small | $0 | ✅ **Do now** (with the bumps) |
| KEDA operator `replicaCount: 2` (§5) | Trivial | ~$0 | ✅ **Do now** (promoted) |
| LiteLLM `allowed_fails` tuning (§5) | Small | $0 | ✅ **Do now** (promoted; careful) |
| Neon pooled endpoint + conn-count alert (§3.2) | Small (config) | $0 | ⏸ **Deferred** — fine at POC scale |
| Decouple CH load from hot path (§4.3.2) | Medium (design) | $0 | ⏸ **Deferred** — wrapper covers the risk; needs DESIGN note |
| On-demand worker fallback pool (§5) | Small (TF) | medium | ❌ **Dropped** — spot-only acceptable indefinitely |
| Regional cluster + node pools (§3.1) | Medium (TF) | **high (~3×)** | ⏸ **Deferred** — single-zone accepted for POC (trigger = leaving POC) |

---

## 8a. Decision log — 2026-06-07 (owner: Victor)

Decisions taken in a walkthrough of every item. These drive the backlog in §9.

| Item | Decision | Notes |
|---|---|---|
| #1 Single-zone cluster (§3.1) | **Accepted (POC)** | Zonal outage = total outage is an accepted ceiling; regional deferred behind a trigger (leaving POC). |
| A. Connection retry/reconnect wrapper (§3.3) | **Do now** | PG + ClickHouse; also delivers the CH-hot-path crash fix (§4.3.1). |
| B. Control/edge replicas:2 + PDBs (§3.4, 4.1, 4.2, 5, 12) | **Do all now** | orchestrator + api + frontend + oauth2-proxy → 2 each, PDBs `minAvailable:1`, hostname topology spread. |
| C. ClickHouse hot-path decoupling (§4.3) | **Retry now (via A); decouple deferred** | Async-load restructure parked as a future DESIGN decision. |
| D. Neon pooled endpoint + conn alert (§3.2) | **Partly done / deferred** | Leader-lock slice DONE (session-mode `_leader_dsn`, forced by the §4.2 split-brain). App-wide pooled DSN + `pg_connections` alert still deferred — fine at POC scale. |
| E. KEDA operator replicaCount:2 (§5) | **Do now** | Promoted from "soon" — bundle with Phase 1. |
| F. LiteLLM `allowed_fails` tuning (§5/§10) | **Do now** | Promoted from "soon"; keep the digest pin; tune carefully (preserve fail-fast intent). |
| G. On-demand worker fallback pool (§5/§11) | **Skip (permanent)** | Spot-only is acceptable indefinitely — eviction is a throughput dip, not data loss. |

**Net Phase-1 slice to implement now:** A + B + E + F (+ retry covers C's cheap fix). Deferred: D,
CH-decouple, regional. Dropped: on-demand worker pool.

## 9. Remediation backlog (actionable)

**Phase 1 — APPROVED + IMPLEMENTED (decision log §8a: A + B + E + F):**
- [x] **[A]** Connection-resilience helper in the storage tier: `control._run` (bounded-backoff retry
      on `OperationalError`/`InterfaceError` over a bounded `psycopg_pool` pool that liveness-checks a
      connection on checkout, behind a `_ConnProxy` so call sites are unchanged) and the mirror
      `analytics._run` (drops + rebuilds the CH client on failure). Covers C's cheap fix too. (§3.3, §4.3.1)
- [x] **[B]** `eval-engine-orch` → `replicas: 2` (leader-elected; warm standby) + stale comment removed.
      (§4.2)
- [x] **[B/D]** Fix the split-brain this exposed: the leader advisory-lock connection now uses a
      session-mode endpoint (`control._leader_dsn`), since the app's pooled `-pooler` DSN defeats
      `pg_try_advisory_lock`. Required for `replicas: 2` to be safe. (§4.2 gotcha)
- [x] **[B]** `eval-engine-api`, `eval-engine-frontend`, `oauth2-proxy` → `replicas: 2`. (§3.4, §4.1, §5)
- [x] **[B]** `PodDisruptionBudget` (`minAvailable: 1`) for api, orch, frontend, oauth2-proxy, litellm;
      `minAvailable: 2` for the ClickHouse/Redis 3× StatefulSets (preserve quorum). (§5, §12)
- [x] **[B]** `topologySpreadConstraints` (hostname, `ScheduleAnyway`) on api/orch/frontend/oauth2-proxy/
      litellm; zone-spread N/A under the single-zone POC. (§4.1)
- [x] **[E]** KEDA operator `operator.replicaCount=2` in `helm.tf` (leader-elects internally). (§5)
- [x] **[F]** LiteLLM `allowed_fails: 3` + `cooldown_time: 30` so one bad upstream no longer downs the
      deployment; `num_retries: 0` (fail-fast) and the pinned digest kept. (§5/§10)

**Deferred (on the backlog, not now — decision log §8a):**
- [~] **[D]** Pooled endpoint work. DONE: the leader connection is pinned to a session-mode endpoint
      (`_leader_dsn`) — forced by the §4.2 split-brain. STILL DEFERRED: the app already runs on the
      pooled `-pooler` DSN, but a `pg_connections()` alert below Neon's limit is not yet wired —
      fine at POC scale; trigger to revisit: connection count climbs toward Neon's limit. (§3.2)
- [ ] **[C]** Decouple the ClickHouse load from the worker hot path: flip ledger→`done` on PG commit,
      load CH asynchronously via the `loaded` flag + `_batch_load` sweep. **Deferred** — needs a DESIGN
      note on the relaxed invariant; A's retry covers the immediate risk. (§4.3.2)
- [ ] ~~`regional_ha=true` Terraform toggle~~ — **deferred: single-zone accepted for the POC** (§3.1).
      Trigger: leaving POC / taking on an availability SLO. ~3× always-on cost when enabled.

**Dropped (decision log §8a):**
- [x] ~~On-demand worker fallback pool~~ — **won't do.** Spot-only is acceptable indefinitely; eviction
      is a throughput dip, not data loss (lease-reclaim preserves correctness). (§5/§11)

**Phase 1 removes every cheap SPOF in the control/edge tier** and makes the platform ride out transient
Postgres/ClickHouse hiccups instead of crash-looping — at essentially no added cost.
