# Eval Engine — Admission & Scheduling (v1)

> Companion to `DESIGN.md` (§8 admission/scheduling) and `ORCHESTRATION.md` (the Orchestrator loop).
> Defines how v1 decides **which runs may make progress, and how much**, so small iteration runs aren't
> starved behind big batch runs — using constants + an admission comparator, **not** a per-tick
> allocator. The deferred weighted-fair-share control loop (and why it's deferred) is in
> `docs/FUTURE.md` §2.

---

## 1. What v1 needs (and doesn't)

At ~1000 runs/day with peaks ~50 concurrent runs, mostly one team, there is almost no **weighted team
contention** to arbitrate — so v1 does **not** build a fair-share allocator. The real risk at this scale,
given a workload that mixes **full runs** and **small subset runs for fast iteration**, is **head-of-line
latency**: a quick 50-sample iteration run queued behind big full-dataset runs.

v1 solves exactly that, cheaply:

- **A global admission cap** bounds the number of concurrently *running* runs (blast radius + shared
  provider quota).
- **A fixed per-run concurrency cap** (`max_inflight`) is the **load-bearing fairness primitive** — a big
  run can't eat the cluster, so any newly-admitted run always makes progress.
- **Two lanes** with a **reserved, borrowable interactive slice** guarantee a quick run gets slots *now*.

No `run_slots` table on the claim path, no per-tick recomputation.

---

## 2. Two-lane admission

- **Classification at launch.** A run is `interactive` or `batch` — by size (e.g. `total_samples ≤ N`, or
  a `limit`/`dataset_slice` set ⇒ interactive) or an explicit flag. This matches how people work:
  "subset to check something" vs "full eval."
- **Reserved interactive slice.** A fixed fraction of capacity (~20–30%) is reserved for the interactive
  lane that batch runs **cannot** consume. This is the actual guarantee — without a reservation, a
  two-lane split alone still lets batch fill everything first.
- **Per-lane admission cap + per-run cap.** Each lane has its own `max_concurrent_runs` and a fixed
  `max_inflight` per run. Interactive uses a small `max_inflight` (many runs progressing → low latency for
  many concurrent iterators); batch uses a larger one (fewer, bigger runs).
- **Work-conserving — borrow + yield.** A *hard* reserve idles capacity whenever no interactive run exists
  (a real duty-cycle waste at 1000/day), so the reserve is **borrowable**: when the interactive lane is
  empty, batch may be admitted into the reserved slots; when an interactive run arrives, batch **yields by
  attrition** — stop admitting batch into the reserve, never kill running samples.

> **Guardrail:** borrow + yield live entirely at the **admission boundary**, never mid-run preemption.
> That line is what keeps the deferred fair-share allocator (`FUTURE.md` §2) from creeping back in through
> the side door.

---

## 3. Enforcement at the claim layer

The per-run cap is enforced with the plain claim (`SCHEMA.md` §1.6) — no sharding, no slot table:

```sql
WITH next AS (
  SELECT run_id, sample_id
  FROM sample_tasks
  WHERE run_id = $run
    AND (status='queued' OR (status='running' AND lease_expires_at < now()))   -- reclaim dead leases
    AND (not_before IS NULL OR not_before < now())                             -- poison-sample backoff
  ORDER BY sample_id
  LIMIT LEAST($batch, $headroom)        -- headroom = max_inflight − running_count (clamped ≥ 0)
  FOR UPDATE SKIP LOCKED
)
UPDATE sample_tasks t SET status='running', claimed_by=$worker, attempts=attempts+1,
       lease_expires_at = now() + $lease, updated_at = now()
FROM next WHERE t.run_id=next.run_id AND t.sample_id=next.sample_id
RETURNING t.run_id, t.sample_id;
```

A run at its `max_inflight` yields no claims, so workers naturally flow to runs with headroom. `headroom`
comes from a **constant** (`max_inflight`) in v1 — not a per-tick budget. Minor between-tick overshoot is
harmless (this is soft admission, not hard preemption).

**Why no sharded claim.** Claim QPS = `throughput / batch_size` ≈ 8 claims/s at batch 50 — a single
Postgres laughs at it. `not_before` already handles poison-sample head-of-line. Hash-sharding is a
deferred, purely-additive optimization for a different workload shape (batch≈1, sub-second samples,
thousands of single-run claimers) — `FUTURE.md` §8.

---

## 4. KEDA scale signal

Workers are a KEDA-autoscaled K8s Deployment. v1 scales on **`count(queued)`** with a sane
**`maxReplicas`** cap (so the worker count never outruns what the model-serving tier can feed — stopping
the 429-thrash / idle-poll failure). Below the cap, worker count tracks queued work; at the cap, extra
demand waits cheaply in the ledger rather than in over-provisioned pods.

The richer "scale to the *admittable* in-flight count" signal (derived from model-serving capacity) is
part of the deferred fair-share Scheduler — `FUTURE.md` §2.

---

## 5. Where this sits

Admission is one function of the single **Orchestrator** singleton (`ORCHESTRATION.md` §2): each tick it
admits runs (two-lane), expands admitted runs into the ledger, reconciles progress, enforces budget, and
finalizes. Admission is kept a distinct function so that, when weighted fair-share is eventually needed,
upgrading is additive — the two fixed lanes become N weighted classes and the constant `max_inflight`
becomes a computed `slot_budget`, with no change to the claim's shape.
