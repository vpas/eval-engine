# Eval Engine — Orchestrator & Ledger State Machine (v1)

> Companion to `DESIGN.md` and `docs/SCHEMA.md`. This is the **core custom IP** — the part most likely
> to harbor correctness bugs — so it's specified in detail: the run lifecycle FSM, the claim/lease
> semantics, the commit protocol, budget, cancellation, finalization, and the failure-mode walkthrough.
> Admission (two-lane) is in `docs/SCHEDULER.md`; deferred work (weighted fair-share) is in
> `docs/FUTURE.md`.

---

## 1. Design stance

**The Postgres ledger is the single source of truth for execution state.** No critical
coordination state lives in worker memory. Any component can die at any time; recovery is
"re-derive from the ledger." This is the whole reason we chose a ledger over an in-memory
controller — we lean on it fully here.

Three correctness goals drive everything below:
1. **No completed sample is ever lost** (a crash never discards finished work).
2. **No sample's *effects* are double-counted** (at-least-once execution → effectively-once results).
3. **Every state is crash-recoverable and idempotent on re-entry.**

---

## 2. Components & responsibilities

| Component | Cardinality | Owns |
|---|---|---|
| **Control Plane (FastAPI)** | N replicas | Accept launch → write `Run(queued)`; expose status/cancel; never touches execution. |
| **Orchestrator** | 1 active (leader-elected) | One "derive state each tick" loop: **admit** (two-lane, `SCHEDULER.md`) → **expand** dataset→ledger → **reconcile** progress/state transitions → **enforce budget**, honor cancel → **finalize** + prune. |
| **Workers (K8s Deployment, KEDA-scaled)** | many | **Claim** a shard → run one Inspect Task → **async-insert results straight to ClickHouse** + transcript→S3 → flip ledger rows `done`. |

> The Orchestrator is a single leader-elected singleton (Postgres advisory lock). It's lightweight
> (queries + transitions, not compute), so one active instance is fine; a standby takes over on lease
> expiry. Admission and reconciliation are kept as **distinct functions** within the one loop, so if
> weighted fair-share is ever needed (`FUTURE.md` §2) splitting it back out is a refactor, not a
> redesign. Workers scale horizontally and async-insert results directly — there is **no separate
> result-loader** (ClickHouse buffers server-side).

---

## 3. Run lifecycle FSM

```
        launch (API)
            │
            ▼
        ┌────────┐  admission ok    ┌───────────┐  ledger fully written  ┌─────────┐
        │ queued │ ───────────────► │ expanding │ ─────────────────────► │ running │
        └────────┘   (Orchestrator)    └───────────┘      (Orchestrator)        └────┬────┘
            │                                                                  │
            │ admission denied / invalid                  all tasks terminal   │  (Orchestrator)
            ▼                                             & all loaded         ▼
        ┌────────┐                                                       ┌────────────┐
        │ failed │ ◄──── fatal orchestration error (any state) ──────────│ finalizing │
        └────────┘                                                       └─────┬──────┘
                                                                               │ aggregates written,
        ┌───────────┐   cancel request / hard budget breach                    │ ledger pruned
        │ cancelled │ ◄──── (Orchestrator, from queued/expanding/running)        ▼
        └───────────┘                                                    ┌───────────┐
                                                                         │ completed │
                                                                         └───────────┘
```

### Transition table

| From → To | Trigger | Who | Crash-recovery / idempotency |
|---|---|---|---|
| queued → expanding | admission: under concurrency cap, budget pre-check ok, RunSpec valid | Orchestrator | Re-pick if still `queued`; transition is a single conditional UPDATE. |
| expanding → running | ledger row count == expected `total_samples` | Orchestrator | Expansion is idempotent (`INSERT … ON CONFLICT DO NOTHING`); on crash, resume and re-check count. |
| running → finalizing | `done+failed == total_samples` (each `done` ⟹ durable in CH, §5.1) **and** failed ≤ threshold | Orchestrator | Condition is a pure query; safe to re-evaluate. |
| running → failed | failed_samples > `max_failed` (fail-fast) | Orchestrator | Idempotent set; workers stop via cancel flag. |
| running → cancelled | user cancel **or** hard budget breach | Orchestrator | Idempotent; drain logic below. |
| finalizing → completed | aggregates computed + ledger pruned | Orchestrator | Finalization fully idempotent (recompute, re-prune are no-ops on re-entry). |
| any → failed | unrecoverable orchestration error | Orchestrator | Terminal; records `error`. |

---

## 4. The result path (execution → ClickHouse → ledger flip → prune)

The ledger is **skinny** — it carries only coordination (status/lease/attempts/error). Results never
pass through widened ledger rows or a separate loader; workers **async-insert straight to ClickHouse**.

ClickHouse performs badly with many tiny inserts (~1B/month) and we need exactly-once analytics
despite at-least-once execution. The path:

```
 worker runs a SHARD (one Inspect Task)
        │ 1. async-insert the shard's results → ClickHouse  +  transcripts → S3
        ▼                                      (server-side buffering; no tiny inserts)
 ClickHouse sample_results  (ReplacingMergeTree keyed (run_id,sample_id), version = load time)
        │ 2. flip the shard's ledger rows status='done'   (tiny coordination update)
        ▼
 (at finalize) compute metrics ONCE over the deduped run partition → run_summary; prune ledger
```

Why this is exactly-once **and** cheap:
- **Idempotency lives in ClickHouse, not Postgres.** A re-executed task (lease expiry → another
  worker) writes the same `(run_id, sample_id)`; `ReplacingMergeTree` keeps the newest by load
  time — a duplicate re-load collapses, a genuine re-execution's newer result wins. No fat payload
  churns the ledger, which kills the dead-tuple/autovacuum bloat that a widened result row would cause.
- **No insert-time materialized view for headline numbers.** Metrics are computed **once at finalize** over
  the deduped run partition (`FINAL` is cheap — one run ≈ 100k rows) into `run_summary`. In-progress
  numbers come from the **gateway** (cost) + **ledger status counts** (progress) — live data with
  no MV that would double-count a re-load.
- **Async inserts** keep ClickHouse happy without a separate loader or a ledger round-trip for the
  payload.

The ledger therefore stays at its skinny SCHEMA §1.6 shape (status/lease/attempts/error only) — it
is **not** widened with result columns.

---

## 5. Worker loop

```python
while not shutting_down:
    batch = claim_tasks(run_id, n=BATCH)          # §6 atomic claim (FOR UPDATE SKIP LOCKED)
    if not batch:
        sleep(backoff()); continue

    for task in batch:
        if redis.get(f"run:{task.run_id}:stop"):  # §8 fast cancel/budget check (cheap)
            release_to_queued(task); continue
        try:
            with sandbox(task) as sbx:             # agentic: provision tier's ephemeral pod (SANDBOXING §7)
                result = run_inspect(task, sbx)    # Inspect solver+scorer; model calls via LiteLLM,
                                                   #   tool/command execution inside sbx; pod torn down on exit
            commit_result(task, result)           # §5.1 ack-before-flip protocol
        except RetryableError as e:                # 429, timeout, transient tool/network
            if task.attempts >= MAX_ATTEMPTS:
                mark_failed(task, e)               # permanent
            else:
                release_for_retry(task, delay=backoff(task.attempts))   # status='queued', not_before=…
        except BudgetExceeded as e:                # gateway budget reject — terminal, NOT a retry (§8)
            mark_failed(task, e)
        except FatalError as e:                    # malformed sample, unrecoverable
            mark_failed(task, e)
```

A worker claims a **shard** and runs it as **one Inspect Task**.

### 5.1 Commit protocol — ack-before-flip

Per result, the commit is an **ordered, ack-gated, idempotent** write; the ledger flip is the single
commit point and happens **only after ClickHouse durably has the row**:

1. write transcript → S3 at the deterministic key (idempotent, overwrite-safe);
2. async-insert the result → ClickHouse with `async_insert=1, wait_for_async_insert=1` — **block until
   CH durably acks** (the row is in a part, not just buffered);
3. **only then** flip the ledger row `status='done'`.

> **Invariant:** `status='done'` ⟹ the result is durable in ClickHouse — so a crash never leaves a
> `done` row with no result. Crash before any step → ledger stays `running` → lease expires → shard
> re-executes → the S3 re-write hits the same key and the CH re-insert collapses via
> `ReplacingMergeTree((run_id, sample_id))`. Atomicity is **ordering + idempotency** (write data
> durably, then commit the pointer), not a distributed transaction. `wait_for_async_insert=1` still
> server-side-batches across workers (one insert per shard; the ack wait amortizes over N samples), so
> batching is preserved.

---

## 6. Claim / lease / retry semantics

- **Claim**: the `FOR UPDATE SKIP LOCKED` query from SCHEMA §1.6 — batched, no double-claim,
  reclaims lease-expired `running` rows.
- **Lease**: `lease_expires_at = now() + LEASE` (e.g. 10 min, > p99 agentic sample time).
  Long-running agentic samples **heartbeat** to extend their lease so they aren't wrongly
  reclaimed mid-flight.
- **Retry**: bounded `MAX_ATTEMPTS` (e.g. 5) with exponential backoff + jitter; a `not_before`
  column gates re-claim so backoff is honored. Distinguish **RetryableError** (transient)
  from **FatalError** (deterministic) — only the former retries.
- **Permanent failure**: recorded with `error_type`; counts toward `failed_samples` and the
  fail-fast threshold (§7), but the run still completes (partial results are valid).

- **Plain ordered claim + per-run cap.** The claim is the plain `ORDER BY sample_id` query, bounded by
  `headroom = max_inflight − running_count` from the fixed per-run cap (`SCHEDULER.md` §3). At ~8
  claims/s peak there is no thundering herd to engineer around, and `not_before` already removes the
  poison-sample head-of-line. (Hash-bucketing is a deferred, purely-additive option for a different
  workload shape — `FUTURE.md` §8.)
- **Error taxonomy.** `RetryableError` (transient) retries; `FatalError` (deterministic) and the
  terminal **`BudgetExceeded`** (gateway budget-stop, §8) do not.
- **Heartbeat from a dedicated async task** so a blocked synchronous Inspect call can't starve the
  heartbeat → wrongful reclaim; idempotent CH writes make any wrongful reclaim *waste*, not corruption.

---

## 7. Progress tracking & fail-fast

- **No per-sample increment of a hot `runs.done_samples` row** (100k contended writes per run
  is a bottleneck). Instead the **Orchestrator derives counts periodically** (every few seconds)
  per active run:
  ```sql
  SELECT status, count(*) FROM sample_tasks WHERE run_id=$1 GROUP BY status;
  ```
  and writes the rollup to `runs`. Cheap, contention-free, good enough for a progress bar.
- **Live run numbers live on the Postgres `runs` row.** Each tick the Orchestrator writes progress
  (ledger status **counts**) + **cost** (the gateway tally — the skinny ledger no longer carries
  `cost_usd`). A **live score** is read **~once/minute, without `FINAL`**:
  ```sql
  SELECT avg(passed), avg(primary_score) FROM sample_results
  WHERE eval_id = $E AND target_id = $T AND run_id = $X;     -- full sort-key prefix, see below
  ```
  The rare un-merged-duplicate skew is accepted for a *live* gauge (the finalize `run_summary` keeps
  `FINAL`). **The full `(eval_id, target_id, run_id)` prefix is mandatory, not `run_id` alone:**
  `run_id` is the 3rd `ORDER BY` column, so filtering on it alone can't use the primary index → a full
  current-month partition scan (~1B rows). A run is one eval × one model, so the Orchestrator already
  holds `eval_id`+`target_id` and passes the full prefix to hit a tight index range. Clients read live
  *and* final numbers from `runs` (`runs.status` distinguishes them); CH `run_summary` is the finalize
  analytics record.
- **Fail-fast**: if `failed_samples > max_failed` (absolute or fraction, per RunSpec), the
  Orchestrator transitions `running → failed` and sets the stop flag — don't burn budget
  finishing a run that's clearly broken (e.g. bad model creds 429-ing everything).

---

## 8. Budget enforcement

Two layers, defense in depth:
- **Gateway hard cap (authoritative + canonical cost):** LiteLLM tracks spend per `run_id`
  tag. A per-run `max_usd` is enforced *at the gateway* — once exceeded it rejects further calls
  with a **distinct terminal `BudgetExceeded` signal (NOT a 429)**. Workers classify it as
  **Fatal/budget-stop**, so it never burns retries or inflates `failed_samples` (which would trip
  spurious fail-fast). The gateway's per-`run_id` tally is the **single source of truth for cost**
  — workers do not recompute catalog-price cost (no two-sources-of-truth divergence).
- **Orchestrator soft stop (fast + graceful):** the Orchestrator reads the **gateway's** run cost each
  tick (not a ledger re-scan — the skinny ledger no longer carries `cost_usd`); nearing budget it
  **sets `run:<id>:stop` in Redis**, which workers check (§5) and stop claiming — a clean drain.

**Policy (per RunSpec `budget.on_exceed`):** `hard_stop` (default → `running→cancelled`,
partial results kept) or `warn` (annotate run, keep going). Same machinery enforces
per-team budgets at admission (§3 queued→expanding pre-check).

---

## 9. Cancellation semantics

**Default = graceful drain.** On cancel: set `run:<id>:stop`; workers finish their *current*
sample (so no half-done work is wasted, and those results are recorded) but claim no more for
that run; when no `running` tasks remain, Orchestrator sets `cancelled`. Partial results stay
queryable and are loaded to ClickHouse normally.

**Option = `hard_kill`** (RunSpec/cancel flag): abandon in-flight samples immediately (workers drop
the current sample on the stop flag); they're left `running` and simply not recorded, then reclaimed
by lease expiry. Faster stop, wastes the in-flight compute. Drain is the default because it's almost
as fast and loses nothing.

Cancellation from `queued`/`expanding` is immediate (no tasks running yet).

---

## 10. Finalization & pruning

On `running → finalizing`:
1. **Barrier**: all tasks are terminal (`done`/`failed`). Because of the ack-before-flip protocol
   (§5.1), `done` already ⟹ the result is durable in ClickHouse — so there is no separate "loaded"
   wait; reaching the barrier means the data is queryable.
2. **Aggregate** from ClickHouse into `run_summary` (`pass_rate`, mean scores, CIs, tokens) using the
   full sort-key prefix + `FINAL`; cost comes from the gateway tally → write `runs.aggregate_metrics`,
   `total_cost_usd`, etc. Idempotent (pure recompute).
3. **Archive failures, prune the rest**: copy *failed* task rows (small) to a durable
   `failed_task_archive` (run_id, sample_id, error_type, last_error, attempts) for post-hoc
   debugging; then `DELETE FROM sample_tasks WHERE run_id=$1`. Keeps Postgres tiny while
   preserving the only per-task history anyone actually wants later.
4. `finalizing → completed`.

Re-entry after a crash in any sub-step is safe: the barrier re-waits, aggregation recomputes,
archive uses `ON CONFLICT DO NOTHING`, prune is naturally idempotent.

---

## 11. Failure-mode walkthrough (the cases that cause bugs)

| Failure | What happens | Why it's safe |
|---|---|---|
| Worker dies after the ledger flip, before next claim | Task is `done`; lease irrelevant | Result already durable in ClickHouse (ack-before-flip, §5.1). |
| Worker dies after CH insert, before the ledger flip | Task stays `running` → lease expires → re-executed | Re-insert collapses via `ReplacingMergeTree((run_id,sample_id))`; object re-written at same key; ClickHouse sees one row. |
| Two workers both think they hold a task | Impossible | `FOR UPDATE SKIP LOCKED` + lease; second sees it `running`/locked. |
| Orchestrator dies mid-expansion | Standby resumes; `ON CONFLICT DO NOTHING`; count re-checked | Expansion idempotent; running only after full count. |
| Orchestrator dies mid-finalize | Standby re-enters `finalizing` | All finalize steps idempotent. |
| Worker re-inserts a shard after a crash | Async-insert again on reclaim | `ReplacingMergeTree((run_id,sample_id))` collapses the duplicate / keeps the newest; headline metrics are computed once at finalize, so nothing double-counts. |
| Long agentic sample exceeds lease | Heartbeat extends lease | Not wrongly reclaimed. |
| Worker dies leaving an orphaned sandbox pod | **Sweeper** reaps pods whose owning task's lease expired (pod labels carry run/sample id) | No sandbox accumulation; teardown guaranteed (SANDBOXING §7). |
| Whole region/cluster restart | All `running` leases expire; runs resume from ledger | No run-level memory state to lose. |

---

## 12. Open questions
- **Heartbeat granularity** — per-sample-step vs wall-clock timer; interaction with Inspect's own
  timeouts.

(Resolved earlier and now baked into the design: leader election = Postgres advisory locks; no separate
result-loader — workers async-insert directly; CH exactly-once = `ReplacingMergeTree((run_id,sample_id))`
+ finalize-time `run_summary`.)

---

## 13. Admission & scheduling

v1 admission is **two-lane (interactive/batch)** with a borrowable interactive reserve + a fixed
per-run concurrency cap — see **`docs/SCHEDULER.md`**. The `teams.scheduling_weight` column exists in
the schema but is unused in v1; the weighted-fair-share allocator that would consume it is deferred —
**`docs/FUTURE.md`** §2.
