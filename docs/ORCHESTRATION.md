# Eval Engine — Orchestrator & Ledger State Machine (Draft v0.1)

> Companion to `DESIGN.md` v0.2 and `docs/SCHEMA.md`. This is the **core custom IP** — the
> part most likely to harbor correctness bugs — so it's specified in detail. It resolves
> open items §14.5 (budget), §14.6 (cancellation), and SCHEMA §4.3/§4.4 (prune & idempotency).

---

## 1. Design stance

**The Postgres ledger is the single source of truth for execution state.** No critical
coordination state lives in worker memory. Any component can die at any time; recovery is
"re-derive from the ledger." This is the whole reason we chose a ledger over an in-memory
controller (D4) — we lean on it fully here.

Three correctness goals drive everything below:
1. **No completed sample is ever lost** (a crash never discards finished work).
2. **No sample's *effects* are double-counted** (at-least-once execution → effectively-once results).
3. **Every state is crash-recoverable and idempotent on re-entry.**

---

## 2. Components & responsibilities

| Component | Cardinality | Owns |
|---|---|---|
| **Control Plane (FastAPI)** | N replicas | Accept launch → write `Run(queued)`; expose status/cancel; never touches execution. |
| **RunScheduler** | 1 active (leader-elected) | Admission control + dataset→ledger **expansion**; `queued→running`. |
| **Workers (Ray)** | many | **Claim** ledger tasks → run Inspect solver+scorer → commit result to ledger + transcript to object store. |
| **ResultLoader** | 1+ (per active run or sharded) | **Batch-load** done-but-unloaded ledger rows → ClickHouse; mark loaded. |
| **RunReconciler** | 1 active (leader-elected) | Derive progress, drive state transitions, enforce budget, honor cancel, **finalize** + prune. |

> Scheduler and Reconciler are leader-elected singletons (lease in Postgres or K8s `Lease`).
> They're lightweight (they issue queries and transitions, not compute), so one active
> instance each is fine; a standby takes over on lease expiry. Workers and ResultLoaders
> scale horizontally.

---

## 3. Run lifecycle FSM

```
        launch (API)
            │
            ▼
        ┌────────┐  admission ok    ┌───────────┐  ledger fully written  ┌─────────┐
        │ queued │ ───────────────► │ expanding │ ─────────────────────► │ running │
        └────────┘   (Scheduler)    └───────────┘      (Scheduler)        └────┬────┘
            │                                                                  │
            │ admission denied / invalid                  all tasks terminal   │  (Reconciler)
            ▼                                             & all loaded         ▼
        ┌────────┐                                                       ┌────────────┐
        │ failed │ ◄──── fatal orchestration error (any state) ──────────│ finalizing │
        └────────┘                                                       └─────┬──────┘
                                                                               │ aggregates written,
        ┌───────────┐   cancel request / hard budget breach                    │ ledger pruned
        │ cancelled │ ◄──── (Reconciler, from queued/expanding/running)        ▼
        └───────────┘                                                    ┌───────────┐
                                                                         │ completed │
                                                                         └───────────┘
```

### Transition table

| From → To | Trigger | Who | Crash-recovery / idempotency |
|---|---|---|---|
| queued → expanding | admission: under concurrency cap, budget pre-check ok, RunSpec valid | Scheduler | Re-pick if still `queued`; transition is a single conditional UPDATE. |
| expanding → running | ledger row count == expected `total_samples` | Scheduler | Expansion is idempotent (`INSERT … ON CONFLICT DO NOTHING`); on crash, resume and re-check count. |
| running → finalizing | `done+failed == total_samples` **and** all loaded to ClickHouse **and** failed ≤ threshold | Reconciler | Condition is a pure query; safe to re-evaluate. |
| running → failed | failed_samples > `max_failed` (fail-fast) | Reconciler | Idempotent set; workers stop via cancel flag. |
| running → cancelled | user cancel **or** hard budget breach | Reconciler | Idempotent; drain logic below. |
| finalizing → completed | aggregates computed + ledger pruned | Reconciler | Finalization fully idempotent (recompute, re-prune are no-ops on re-entry). |
| any → failed | unrecoverable orchestration error | Scheduler/Reconciler | Terminal; records `error`. |

---

## 4. The result path (execution → ledger → batch-load → ClickHouse → prune)

This is the spine, and its shape is forced by two facts: **(1)** ClickHouse performs badly
with many tiny inserts (we'd be doing ~1B/month), and **(2)** we need exactly-once analytics
despite at-least-once execution. The solution decouples them:

```
 worker executes sample
        │ 1. write structured result INTO the ledger row (UPSERT on PK) + transcript→S3
        ▼
 sample_tasks row: status='done', result columns filled, loaded=false   ◄── source of truth, exactly-once
        │ 2. ResultLoader bulk-reads done & loaded=false, in batches
        ▼
 ClickHouse sample_results  (one big INSERT per batch)                   ◄── analytics projection
        │ 3. mark ledger rows loaded=true
        ▼
 (at finalize) all loaded → compute aggregates → prune ledger            ◄── Postgres stays small
```

Why this works:
- **Dedup happens in Postgres, before ClickHouse ever sees a row.** A re-executed task
  (lease expiry → another worker) UPSERTs the *same PK* `(run_id, sample_id)`, overwriting
  its own prior result. So at most one result per sample exists in the ledger → ClickHouse
  receives no duplicates. *This resolves SCHEMA §4.4 — no ReplacingMergeTree gymnastics needed.*
- **Batched inserts** keep ClickHouse happy (configurable: flush every ~10 s or ~50k rows).
- **Near-real-time analytics**: the loader runs continuously during the run, so Superset
  sees partial results within seconds, not only at finalize.

This requires extending the ledger to carry the result. Amendment to `sample_tasks`:

```sql
ALTER TABLE sample_tasks
  ADD COLUMN passed         smallint,
  ADD COLUMN primary_score  double precision,
  ADD COLUMN scores         jsonb,
  ADD COLUMN tokens_in      int,
  ADD COLUMN tokens_out     int,
  ADD COLUMN cost_usd       numeric(12,6),
  ADD COLUMN latency_ms     int,
  ADD COLUMN error_type     text,
  ADD COLUMN transcript_uri text,
  ADD COLUMN loaded         boolean NOT NULL DEFAULT false;
-- Loader hot path:
CREATE INDEX ON sample_tasks (run_id) WHERE status='done' AND loaded=false;
```

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
            put_object(result.transcript_uri, zstd(result.transcript))   # idempotent: keyed path
            commit_result(task, result)            # UPSERT result cols + status='done' (one txn)
        except RetryableError as e:                # 429, timeout, transient tool/network
            if task.attempts >= MAX_ATTEMPTS:
                mark_failed(task, e)               # permanent
            else:
                release_for_retry(task, delay=backoff(task.attempts))   # status='queued', not_before=…
        except FatalError as e:                    # malformed sample, unrecoverable
            mark_failed(task, e)
```

- `commit_result` is a **single transaction**: it writes the result columns *and* flips
  `status='done'` together. So "result written" and "task done" can never disagree.
- The dangerous interleaving — *transcript/result written, then worker dies before
  `commit_result`* — is safe: the task stays `running`, its lease expires, another worker
  re-executes and UPSERTs over the (orphaned, never-committed) state. The earlier object-store
  write is overwritten at the same key. No duplicate reaches ClickHouse.

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

---

## 7. Progress tracking & fail-fast

- **No per-sample increment of a hot `runs.done_samples` row** (100k contended writes per run
  is a bottleneck). Instead the **Reconciler derives counts periodically** (every few seconds)
  per active run:
  ```sql
  SELECT status, count(*) FROM sample_tasks WHERE run_id=$1 GROUP BY status;
  ```
  and writes the rollup to `runs`. Cheap, contention-free, good enough for a progress bar.
- **Fail-fast**: if `failed_samples > max_failed` (absolute or fraction, per RunSpec), the
  Reconciler transitions `running → failed` and sets the stop flag — don't burn budget
  finishing a run that's clearly broken (e.g. bad model creds 429-ing everything).

---

## 8. Budget enforcement (resolves §14.5)

Two layers, defense in depth:
- **Gateway hard cap (authoritative):** LiteLLM tracks spend per `run_id` tag (passed as a
  header via Inspect). A per-run `max_usd` is enforced *at the gateway* — once exceeded, the
  proxy rejects further calls for that run, so spend physically cannot overshoot by more than
  in-flight requests.
- **Reconciler soft stop (fast + graceful):** the Reconciler sums `cost_usd` from the ledger
  each tick; nearing budget it **sets `run:<id>:stop` in Redis**, which workers check per
  sample (§5) and stop claiming — a clean drain rather than a wall of gateway 429s.

**Policy (per RunSpec `budget.on_exceed`):** `hard_stop` (default → `running→cancelled`,
partial results kept) or `warn` (annotate run, keep going). Same machinery enforces
per-team budgets at admission (§3 queued→expanding pre-check).

---

## 9. Cancellation semantics (resolves §14.6)

**Default = graceful drain.** On cancel: set `run:<id>:stop`; workers finish their *current*
sample (so no half-done work is wasted, and those results are recorded) but claim no more for
that run; when no `running` tasks remain, Reconciler sets `cancelled`. Partial results stay
queryable and are loaded to ClickHouse normally.

**Option = `hard_kill`** (RunSpec/cancel flag): abandon in-flight samples immediately
(Ray cancels the tasks); in-flight samples are left `running` and simply not recorded.
Faster stop, wastes the in-flight compute. Drain is the default because it's almost as fast
and loses nothing.

Cancellation from `queued`/`expanding` is immediate (no tasks running yet).

---

## 10. Finalization & pruning (resolves SCHEMA §4.3)

On `running → finalizing`:
1. **Barrier**: wait until every `done` row has `loaded=true` (ResultLoader caught up).
2. **Aggregate** from ClickHouse (`pass_rate`, mean scores, CIs, total cost/tokens) → write
   `runs.aggregate_metrics`, `total_cost_usd`, etc. Idempotent (pure recompute).
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
| Worker dies after `commit_result`, before next claim | Task is `done`; lease irrelevant | Result already durably committed; loader picks it up. |
| Worker dies after transcript write, before `commit_result` | Task stays `running` → lease expires → re-executed | UPSERT overwrites orphaned state + object at same key; ClickHouse sees one row. |
| Two workers both think they hold a task | Impossible | `FOR UPDATE SKIP LOCKED` + lease; second sees it `running`/locked. |
| Scheduler dies mid-expansion | Standby resumes; `ON CONFLICT DO NOTHING`; count re-checked | Expansion idempotent; running only after full count. |
| Reconciler dies mid-finalize | Standby re-enters `finalizing` | All finalize steps idempotent. |
| ResultLoader double-loads a batch | Mark `loaded=true` only after successful insert; on crash, unmarked rows reload | Dedup already guaranteed upstream (PK upsert), so a reloaded row is byte-identical; CH dedup via `(run_id,sample_id)` ordering collapses the rare repeat, or load is transactional per batch. |
| Long agentic sample exceeds lease | Heartbeat extends lease | Not wrongly reclaimed. |
| Worker dies leaving an orphaned sandbox pod | **Sweeper** reaps pods whose owning task's lease expired (pod labels carry run/sample id) | No sandbox accumulation; teardown guaranteed (SANDBOXING §7). |
| Whole region/cluster restart | All `running` leases expire; runs resume from ledger | No run-level memory state to lose. |

> The one residual: a ResultLoader crash *between* the ClickHouse insert and the `loaded=true`
> mark will reload that batch. Upstream PK-dedup means the reloaded rows are identical, so the
> safe options are (a) transactional batch (insert + mark in one logical unit via a staging
> table + `ALTER … MOVE PARTITION`), or (b) ClickHouse `ReplacingMergeTree(version)` keyed on
> `(run_id,sample_id)` with periodic `OPTIMIZE … FINAL`. **Recommend (b)** — simpler, and
> queries that must be exact use `FINAL`/aggregation; the run-level MV is fed once at finalize
> from deduped data anyway. *(This refines SCHEMA §2 — switch engine to ReplacingMergeTree.)*

---

## 12. Open questions for v0.3
1. ~~**Leader election substrate**~~ — **RESOLVED: Postgres advisory locks** (`pg_try_advisory_lock`) for Scheduler/Reconciler leadership. One fewer dependency; we're already transactional in PG.
2. **ResultLoader sharding** — one loader per active run, or a few global loaders sharded by `run_id` hash? Depends on concurrent-run count.
3. ~~**Scheduling fairness**~~ — **RESOLVED: weighted-fair share per team.** Each team has a weight; the Scheduler allocates capacity (concurrent in-flight sample slots) across runs proportional to team weight, so a high-volume team can't starve others. (See §13.)
4. **Heartbeat granularity** — per-sample step vs wall-clock timer; interaction with Inspect's own timeouts.
5. **CH exactly-once** — ratify ReplacingMergeTree(version=attempt) + `FINAL`-on-exact-queries, vs staging-partition-move. (Leaning ReplacingMergeTree per §11.)

---

## 13. Weighted-fair scheduling (resolved)

When demand (queued/active runs) exceeds capacity (total concurrent sample slots the
cluster can sustain — bounded by worker count and gateway rate limits), the Scheduler
**allocates slots across runs proportional to each owning team's weight**, so no single
team starves the others.

- **Weight** lives on `teams.scheduling_weight` (default 1). A team's fair share of the
  global slot budget = `weight / Σ active-team weights`.
- **Mechanism (max-min weighted fair share):** each tick the Scheduler computes, per active
  run, a target in-flight slot count from its team's share (split across that team's active
  runs), and caps how many tasks workers may hold for each run via a per-run
  `slot_budget` (enforced at claim time: a worker only claims for run R if R is under its
  current `slot_budget`). Unused share spills to teams with backlog (work-conserving).
- **Within a team**, runs share the team's slice by simple FIFO on `queued_at` (or an
  optional per-run priority later).
- **Admission cap** (queued→expanding) still bounds the *total* number of concurrently
  *running* runs; weighted-fair governs slot allocation *among* admitted runs.

This is intentionally a soft, periodically-recomputed allocation (not hard preemption) —
cheap, good enough for fairness, and consistent with the "Reconciler derives state each
tick" model. Strict preemption can come later if needed.
