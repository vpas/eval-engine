Most consequential issues

  1. Ray and the ledger are redundant — you're paying for two schedulers and using each halfway

  DESIGN D4 picks both Ray (KubeRay) and a Postgres claim/lease ledger. But ORCHESTRATION §5 and the prototype README both state plainly: "the workers never
  coordinate… Ray is just run N copies of a loop." That means none of Ray's actual value (task graph, object store, locality-aware scheduling, autoscaling
  of a DAG) is used — Ray is a glorified pod launcher, and the real scheduler is FOR UPDATE SKIP LOCKED.

  You should pick one coordination model:
  - Ledger-as-coordinator (what you actually built) → drop Ray entirely; a plain K8s Deployment of worker pods polling Postgres is simpler, removes a heavy
  dependency and its failure modes, and loses nothing.
  - Ray-as-coordinator → let the driver dispatch tasks and use the ledger only for durability/idempotency, not for work distribution.

  Right now you carry the operational weight of KubeRay and a bespoke durable queue while getting the benefits of neither fully. This is the single biggest
  "why is this here" in the stack.

  2. You hand-built Temporal/SQS and then listed Temporal as the escape hatch

  The claim/lease/retry/visibility-timeout/heartbeat machinery in ORCHESTRATION §5–§9 is exactly what a durable task queue (Temporal, river, SQS, Cloud
  Tasks, even Celery+Redis) gives you off the shelf, battle-tested. The doc names Temporal as a "documented escape hatch" — but the escape hatch's whole job
  (durable per-task state machine with retries) is the part you're building by hand and flagging as "core custom IP… most likely to harbor correctness
  bugs" (ORCH header). That's backwards: the highest-bug-risk component is the one you should not be writing from scratch. Worth a hard look at whether the
  differentiated IP is really the queue, or just the eval-specific result-path/ETL on top of a stock queue.

  3. The ledger carries result payloads — this fights the "keep Postgres small" premise and creates a write-amplification bottleneck

  DESIGN sells the ledger as ephemeral coordination state. But ORCHESTRATION §4 widens sample_tasks to carry the full result (jsonb scores, tokens, cost,
  transcript_uri, …) so the ResultLoader can batch it to ClickHouse. Consequence per sample, at 1B/month: claim UPDATE → commit UPDATE (fat jsonb) → loader
  SELECT → mark-loaded UPDATE → prune DELETE — ~5 row ops, with status changing on indexed columns (so no HOT updates) and a high-churn UPDATE+DELETE
  pattern that generates massive dead-tuple/autovacuum pressure on a single hot table. The bloat risk on sample_tasks at this churn isn't in §12 Risks but
  it's one of the likeliest production fires.

  Cleaner: keep sample_tasks as pure coordination (status/lease/attempts/error only) and move result payloads off the critical row — workers write results
  to a stream (Redis Streams/Kafka) or directly to a ClickHouse async-insert/buffer, with the ledger PK only as the idempotency key. Don't co-locate two
  opposite write patterns (tiny hot coordination updates vs fat append-mostly results) in one table.

  4. The Postgres queue is the true scaling ceiling, and it's treated as free

  - Claim contention: WHERE run_id=$1 … ORDER BY sample_id … FOR UPDATE SKIP LOCKED makes every claimer walk the same ordered head of the same run's rows.
  SKIP LOCKED skips locked rows but still scans past them, so claim cost rises with concurrency — the classic queue-head thundering-herd cliff. 3.8k/s at 12
  workers (prototype) tells you little about hundreds of workers across many runs. Randomized/sharded claim order isn't discussed.
  - Read amplification: the Reconciler re-derives counts via GROUP BY status and sums cost_usd over the live ledger every few seconds per active run (§7,
  §8). With many concurrent 100k-row runs that's repeated near-full scans of the hottest table, on top of the loader's partial-index scans. The "no hot
  counter row" decision (§7) trades write contention for read amplification at scale.
  - A single Postgres is metadata store + 1B-op/month hot ledger + advisory-lock leader election + budget aggregation. That's a lot of distinct load
  profiles on one box. The cost sum especially should come from the gateway (which §8 already calls authoritative for spend), not from re-scanning the
  ledger.

  ---
  Correctness bugs

  5. ReplacingMergeTree + the run_metrics MV double-count on loader re-load

  ORCH §11 recommends ReplacingMergeTree(attempt) to mop up a ResultLoader crash-between-insert-and-mark re-load. But SCHEMA §2 also feeds a run_metrics
  AggregatingMergeTree via a materialized view. MVs in ClickHouse fire per inserted block — they do not see the later ReplacingMergeTree merge/collapse. So
  a duplicated loader batch is collapsed in the base table but already double-counted in run_metrics (pass_rate, total_cost). The two dedup mechanisms don't
  compose. This silently corrupts exactly the headline numbers the dashboard reads.

  Also, attempt as the version column does nothing useful here: the re-load case produces identical rows (same attempt), and a genuine retry's orphaned
  attempt was never committed/loaded — so versioning by attempt never discriminates. The honest fix is the staging-partition-move (transactional batch) you
  dismissed in §11, or async-insert dedup tokens — not RMT-plus-MV.

  6. Gateway budget rejection collides with the retry classifier

  §8 makes the LiteLLM hard cap reject in-flight calls once max_usd is exceeded. A rejected model call surfaces to the worker as a 429-ish error — which
  ORCH §5/§6 classifies as RetryableError, so the worker backs off and retries up to MAX_ATTEMPTS, burning attempts and possibly recording budget-stop as
  transient failure (inflating failed_samples → spurious fail-fast). Budget rejection must be a distinct terminal signal (stop/Fatal), not folded into the
  transient bucket. The interaction is unspecified and currently looks wrong.

  Relatedly, you have two cost sources of truth: worker-computed cost_usd (catalog price × tokens, summed by the Reconciler) and the gateway's per-run_id
  tally. These will diverge (catalog drift, cached tokens, provider-side pricing). Pick the gateway as canonical and have the ledger/CH carry it, rather
  than recomputing.

  7. Expansion can wedge a run forever on duplicate sample_ids

  expanding→running fires when count(*) == total_samples, and expansion is INSERT … ON CONFLICT DO NOTHING on PK (run_id, sample_id). If a dataset (JSONL/HF
  import) has any duplicate sample_id, DO NOTHING silently drops the dupes, the count never reaches total_samples, and the run is stuck in expanding
  indefinitely. There's no sample_id-uniqueness validation at dataset snapshot time. Either validate uniqueness when content-addressing the snapshot, or
  compute total_samples from the deduped inserted count, not the raw sample count.

  ---
  Overstated claims

  8. "Fully reproducible from the RunSpec" is reproducible inputs, not results

  DESIGN lists reproducibility as a core goal/NFR and §9.9 sells "re-run clones the RunSpec → identical pinned inputs." But hosted models are
  non-deterministic and silently versioned server-side; temperature=0 and seed don't guarantee determinism on most provider APIs; LLM-judge scorers are
  themselves non-deterministic model calls; and many pinned model_ids will be deprecated well inside the 12-month retention window. code_ref pins your
  plugin code but not Inspect/LiteLLM/provider SDK versions (only the worker image does, and only if images are retained). This is real spec-reproducibility
  (same inputs, statistically comparable outputs), not bitwise result-reproducibility. Reframe the NFR honestly — otherwise users will treat a re-run's
  score delta as a bug.

  9. "Single LiteLLM egress for all traffic" conflates two very different concerns

  Routing external API providers through LiteLLM (key mgmt, global rate limit, cost cap) is clearly right. Forcing self-hosted vLLM through the same Python
  proxy is questionable: for in-cluster inference you control, you want max throughput and capacity-based load-balancing, not a Redis-round-trip-per-call
  rate limiter and an extra serialization hop. At "thousands of calls/s peak" a Python proxy is a throughput ceiling and SPOF (≥2 replicas mitigates
  availability, not the per-call Redis latency). "Rate limiting" for self-hosted is really capacity/concurrency control, which vLLM + a normal LB does
  better. Consider: gateway for external egress only; direct LB'd routing to vLLM with a concurrency limiter. The doc presents single-egress as
  self-evidently good; it's a genuine tradeoff.

  ---
  Scale/ops concerns under-weighted

  10. Pod-per-sample sandboxing is the hardest unsolved problem, and it's an "open question"


  SANDBOXING mandates an ephemeral pod per agentic sample, "thousands of create/destroy/s." Realistic K8s pod-churn ceilings (kube-scheduler, kubelet,
  Cilium IPAM, gVisor/Kata boot, image) are low-hundreds/s per cluster — below your 385/s aggregate once a meaningful agentic fraction hits. Warm pools (§8)
  are the mitigation, but warm gVisor/Kata pods have unbounded idle cost and the sizing is open question #1 — i.e., the dominant scaling risk is explicitly
  unresolved. Worth evaluating microVM-in-pool reuse (firecracker-containerd) or a recycling sandbox service over full per-sample pod lifecycle. This
  belongs in DESIGN §12 Risks as a top-tier item, not buried as a sandboxing open question.

  11. You bypass Inspect's own sample loop — Pure-A friction not acknowledged

  Inspect's natural unit is a Task (a dataset of samples) with its own concurrency control, retries, epochs, and one .eval log per task. By driving Inspect
  per single sample (one sample per worker claim), you discard Inspect's intra-task batching/retry and pay per-sample Task/registry/log-init overhead — and
  you get one .eval fragment per sample rather than the per-run artifact §6's diagram implies (runs/<run_id>/eval.log). This is the same "use the tool
  halfway" smell as the Ray issue: you adopt Inspect as the kernel but bypass the part of it that does distribution. Either let a worker run a shard of
  samples through one Inspect Task (amortize startup, reuse its concurrency — at the cost of coarser ledger granularity), or explicitly accept and document
  the per-sample overhead and the .eval-fragment-merge step.

  12. Transcript "keep-all 12mo" is a named cost bomb deferred for simplicity

  The doc says transcript storage is "the dominant cost," then chooses keep_all 12-month "for simplicity" (D8) and defers tiering. 1B transcripts/month ×
  12mo, even zstd'd, is enormous, and the obvious lever — keep all failures + a sample of passes — is exactly the kind of policy that's painful to retrofit
  (you can't recover transcripts you chose to keep but later wish you'd sampled differently; and you can't reclaim cost already spent storing them).
  Sampling-by-default with per-eval opt-in to keep-all is the safer default for the thing you've identified as your biggest cost.

  ---
  Smaller flaws

  - group_key as a single LowCardinality(String) (SCHEMA §2) forces concatenation for real multi-dimensional slicing (subject × difficulty × language),
  which kills independent slicing and is in the ORDER BY/partitioning critical path — painful to retrofit. Make it Map(String,String) or known dim columns
  now (it's already flagged as open Q#1, but it's an early-binding decision, not a later one).
  - The ClickHouse projection can't filter on input/prompt content — inputs live only in the S3 transcript. Any "find samples where the prompt contained X"
  analysis requires fetching transcripts. Probably fine, but worth stating; many eval analyses slice on input features.
  - Heartbeat-to-extend-lease (§6) adds mid-sample Postgres writes for every long agentic sample, and if a worker is blocked inside a synchronous Inspect
  call it may miss its heartbeat → wrongful reclaim → duplicate sandbox + model spend. Cadence-vs-lease is open question #4 — central to agentic
  correctness, still unresolved.
  - Ordered claim + retry head-of-line: ORDER BY sample_id means a poison sample at the front gets re-claimed every pass until attempts exhaust; not_before
  mitigates but doesn't eliminate the head-of-line effect.

---
---

# Response to the review (decisions made)

Thanks — this was a genuinely sharp read of the design. We worked through every point and adopted
most of them; a couple we reframed as tradeoffs rather than flaws, and one was already handled in
the prototype but mis-stated in the docs. All decisions are now baked into **DESIGN §15
(amendments A1–A13)** and threaded through ORCHESTRATION / SCHEMA / SANDBOXING. Point by point:

**1. Ray vs. the ledger — AGREED, dropped Ray.** You're right that the prototype proved workers
never coordinate, so Ray's DAG/object-store/locality value went unused and `FOR UPDATE SKIP LOCKED`
was the real scheduler. Decision (**A1**): workers become a plain **K8s Deployment, autoscaled by
KEDA** on ledger queue depth (`count(status='queued')`), scaling to zero between runs. The ledger
stays the sole coordinator. This drops the entire KubeRay operator with no loss of capability. Ray
is kept as a documented future option only if a genuinely Ray-shaped (stateful/DAG) workload appears.

**2. Hand-built queue vs. Temporal/SQS — AGREED on the principle; the escape hatch was wrong.** Your
"backwards" framing is correct — but the fix isn't Temporal (a multi-step *workflow* engine for a
single-step fan-out; at 1B/mo its own persistence becomes the same bottleneck, and it can't answer
our SQL-shaped control-plane questions: per-run progress, weighted-fair-share, cancel-by-run,
re-run-failed). SQS/Cloud Tasks solve only the narrow claim and don't replace the ledger (you'd run
both → two sources of truth). Decision (**A2**): keep a **thin** Postgres claim, and name **pgmq /
procrastinate** (Postgres-native, portable) as the real escape hatch if the lease/reclaim code bites.
Crucially, the result-path redesign (A3) shrank this surface to claim+lease+status, and idempotency
(A4) downgrades a wrongful reclaim from corruption to wasted spend. Temporal removed from the doc.

**3. Ledger carries result payloads — AGREED (one of the best catches).** Co-locating tiny hot
coordination updates with fat append-mostly result rows on one high-churn table is exactly the
dead-tuple/autovacuum fire you describe. Decision (**A3**): the ledger is now **skinny** (status/
lease/attempts/error only). Workers **async-insert results straight to ClickHouse**; the ledger row
just flips `done`. The `sample_tasks` widening in ORCH §4 is reverted.

**4. Postgres as the scaling ceiling — AGREED on substance.** Decision: **hash-sharded claim**
(`hashtext(sample_id) % N`) removes the queue-head thundering-herd and the poison-sample head-of-line
(**A6**); the Reconciler no longer sums cost over the ledger (gateway is canonical, **A5**) and
reads only status counts from the now-skinny table; result writes left PG entirely (**A3**). Net, PG
drops from four load profiles to two (metadata + skinny coordination + advisory-lock leader election).

**5. ReplacingMergeTree + MV double-count — AGREED (the subtlest catch).** Correct that the MV fires
per insert block and never sees the RMT collapse, so a re-loaded batch double-counts the headline
numbers, and `attempt`-as-version doesn't discriminate a re-load. Decision (**A4**): RMT keyed on
**`(run_id, sample_id)`** with **version = load time** (which does discriminate); and **headline
metrics are computed ONCE at finalize** over the deduped run partition into a `run_summary` table —
**no insert-time MV**. Live in-progress numbers come from the gateway (cost) + ledger status counts.

**6. Budget reject vs. retry classifier — AGREED.** Decision (**A5**): the gateway returns a
**distinct terminal `BudgetExceeded`** signal (not a 429); workers map it to Fatal/budget-stop, so
it never burns attempts or inflates `failed_samples`. And we **pick the gateway as the single source
of truth for cost** — workers stop recomputing catalog-price cost (the prototype's catalog pricing
was always a LiteLLM stand-in).

**7. Duplicate `sample_id` wedges a run — AGREED.** Decision (**A7**): **validate `sample_id`
uniqueness at dataset-snapshot time** and reject duplicates with a clear error (silent dedup is the
other half of the bug); and make `expanding→running` fire on expansion *completion* with
`total_samples` = the deduped inserted count, not a `count(*) == raw_total` race.

**8. "Fully reproducible" overstated — AGREED.** Decision (**A8**): reframed to **reproducible
inputs / comparable outputs**. We now also pin the **worker image digest** (which actually fixes the
Inspect/LiteLLM/SDK-version gap `code_ref` left open) and capture the **provider version-fingerprint**
per sample so re-run deltas are attributable; **epochs** are the statistical-comparability mechanism.
Noted that self-hosted vLLM (pinnable weights) is genuinely more reproducible than hosted APIs.

**9. Single egress for self-hosted vLLM — AGREED it's a tradeoff (we'd called it self-evident).**
Decision (**A9**): the LiteLLM gateway is mandatory for **external** traffic only. Self-hosted vLLM
routes **direct, capacity-LB'd, with concurrency control** (rate-limiting is the wrong control for
GPU capacity; a Python proxy in the hot path is a throughput ceiling + SPOF). But cost/budget/
observability stay **unified** — the worker reports vLLM usage to the same accounting plane, so we
keep the control-plane value without the hot-path proxy. (Future-phase; everything is external today.)

**10. Pod-per-sample sandbox churn — AGREED, and elevated.** Your structural point (it was buried as
an open question) is the most important. Decision (**A10**): the default becomes a **pooled,
reset-reuse sandbox service** — long-lived sandboxes reset between samples (overlay-fs rollback, kill
procs), so the per-sample op is an in-sandbox reset that **never hits kube-scheduler/kubelet/IPAM**
(the actual ceiling). MicroVM snapshot-restore (Firecracker) for strong-isolation-at-churn.
**Tier-gated: T1/T2 reuse, T3 single-use.** (Note: a warm pool of *single-use* pods hides boot
latency but not create/destroy throughput — it doesn't move the ceiling.) Promoted to a **top-tier
DESIGN §12 risk**, explicitly gated on a churn spike before agentic-at-scale.

**11. Bypassing Inspect's sample loop — AGREED, partly already handled.** The prototype already runs
a **claimed shard as one Inspect Task** (`_execute_batch` → one `inspect_eval` over the batch), so
the per-sample-overhead critique applies to the doc wording, not the build — now aligned (**A11**).
One reframe: Inspect's parallelism is in-process async concurrency *within* a Task (which we use); it
isn't a multi-node scheduler (which the ledger provides) — so there's no multi-node Inspect to
"bypass." The legitimate residuals are addressed: the per-run `.eval` artifact is defined as a
**collection of per-shard logs**, and **shard size** is documented as a tuning knob (amortization vs
crash-blast-radius vs lease).

**12. Transcript keep-all 12mo — AGREED (the irreversibility is the crux).** Decision (**A12**): flip
the default to **sample-by-default** — keep all failures + a stratified-by-dimension sample of passes,
with per-eval opt-in `keep_all` — plus **storage-class tiering** (Standard→Nearline→Coldline). The
asymmetry is the argument: keep-all's sunk storage cost isn't reversible-downward, while sampling is
reversible-upward, and agentic transcripts (which dominate the bill) are exactly where sampling helps
most. The ClickHouse projection stays full-fidelity.

**Smaller flaws:**
- *group_key* — AGREED, early-binding. Decision (**A13**): `dimensions Map(String,
  LowCardinality(String))` (+ `category` first-class) for independent multi-dim slicing; `ORDER BY`
  keyed on stable access patterns (eval/run), so the irreversible sort-key isn't bound to volatile
  dimensions; hot dims promoted to materialized columns + skip indexes later **without a rewrite**.
- *CH can't filter on input content* — AGREED (low severity). We add an `input_hash` and push
  sliceable **input features into `dimensions` at ingest**; full prompt text stays in the transcript,
  not the analytics table. True full-text search remains a transcript job (out of scope v1).
- *Heartbeat → wrongful reclaim* — AGREED. A **dedicated async heartbeat task** (so a blocked
  synchronous Inspect call can't starve it) + generous agentic leases; and idempotent CH writes make
  a wrongful reclaim *wasted spend*, not corruption.
- *Ordered-claim head-of-line* — AGREED; resolved by the hash-sharded claim (A6) + `not_before`
  backoff + `max_attempts`→archive.

Net: the review moved us off two locked decisions (D4 Ray, D8 keep-all), fixed a real correctness
bug (the RMT+MV double-count), and elevated the agentic-churn risk to top-tier. Much appreciated.

---
---

# Second-round review (assessment of the A1–A13 response)

Verified the response landed, not just narrated: DESIGN §15 (A1–A13) is in the doc, the decision
log is amended, and `_execute_batch` (runner.py:86) really runs a claimed shard through one
`inspect_eval`. The claims are real.

**Verdict.** Strong response, and the pushbacks are *correct* rather than capitulation: Temporal is
the wrong shape (workflow engine for a single-step fan-out); Inspect's concurrency is in-process-
within-a-Task, not a multi-node scheduler, so there's nothing to "bypass"; and "a warm pool hides
boot latency but not create/destroy throughput" is exactly the right rebuttal on sandbox churn.
Whoever wrote this understood the critique. **But the biggest structural fix (A3, the skinny ledger)
moved a load-bearing property out of Postgres without fully re-homing it, opening one genuinely new
correctness gap. Three other amendments are sound *directions*, not yet *resolutions*.**

Scorecard of the original 13: **7 cleanly resolved · 3 resolved-but-introduce-a-new-issue (A3, A4,
A10) · 3 turned into well-understood-but-deferred/under-specified directions (A9, A1/KEDA, A6).**

## Cleanly resolved (no residual)
A5, A7, A8, A11, A13, A1, A2. Done.

## New issues the fixes introduce

**1. A3 trades a double-count risk for a LOST-sample risk — the blocker.** The entire exactly-once
argument in original ORCHESTRATION §4 was "dedup happens in Postgres via PK upsert *before*
ClickHouse sees a row." A3 deletes that: workers now async-insert results straight to ClickHouse,
then flip the ledger row to `done`. Dedup is now eventual (A4's ReplacingMergeTree). Fine for
duplicates — but the *other* crash order is worse: if a worker flips `done` **before** ClickHouse
durably acks (i.e. `wait_for_async_insert=0`, the fast/default mode) and the CH buffer is lost
before flush, you get a ledger row marked `done` with **no result in ClickHouse → a silently lost
completed sample**, violating correctness goal #1 (which the old PK-upsert path guaranteed). Required
fix, specific and mandatory: **CH insert with `wait_for_async_insert=1`, acked, THEN flip the
ledger.** The commit path is now a 3-way write (CH + S3 transcript + PG flip) whose ordering/
idempotency must be specified as carefully as §5 once was. The skinny ledger didn't remove the
commit complexity — it pushed it into a distributed 3-way commit. This must be written down before
A3 counts as resolved.

**2. A4 silently kills live SCORE metrics.** Dropping the insert-time MV fixes the double-count, but
live numbers now come from "gateway (cost) + ledger status counts (progress)" — which covers cost
and progress but **not pass-rate/accuracy** (a score aggregate that now only materializes in
`run_summary` at finalize). For a 100k-sample multi-hour run the dashboard can no longer show current
accuracy. Recoverable by querying CH live with `FINAL`/`argMax(version)`, but that path isn't named;
"live numbers from gateway+status" reads as covered when it isn't. Define the live-score query path
or accept a long-run dashboard regression.

**3. A10 resolves throughput but opens a T2 isolation question, and is deferred.** Reset-reuse
(overlay-fs rollback + kill procs) is a security *downgrade* from single-use pods: it reintroduces
cross-sample contamination risk. Rollback covers the overlay; it does NOT cover kernel state under a
shared-kernel runtime (gVisor), surviving processes the kill missed, or an established outbound
connection. T3 staying single-use handles the adversarial case — but **T2 is where general untrusted
model code runs**, and T2 now does reuse. "T1/T2 reuse" is asserted, not shown safe. Cleaner: commit
T2 to **microVM snapshot-restore** (fresh kernel per restore → concern evaporates) rather than
leaving reset-reuse and snapshot-restore as interchangeable. Also note A10 is gated on "a churn spike
before agentic-at-scale" and the prototype comment (runner.py:91) still describes the old per-sample-
pod model — so its status is "deferred with direction," not "built."

## Still open / under-specified

**4. A9 removed the global vLLM capacity coordinator without re-homing it.** §2 called global cross-
worker rate limiting for self-hosted "mandatory." A9 correctly routes vLLM off the gateway — but
"capacity-LB'd with concurrency control" needs a home for the *global* concurrency budget across
autoscaled workers. Per-worker limits aren't global; a shared Redis limiter is the per-call round-
trip A9 was avoiding. Mechanism unspecified. (Fine to defer — all traffic is external today — but
it's a gap, not a resolution.)

**5. A1's KEDA scale signal is naive.** Scaling worker count on raw `count(status='queued')` ignores
the gateway rate cap and the weighted-fair-share slot budget (ORCH §13). If the gateway rate limit is
the real ceiling, KEDA over-provisions workers that thrash on 429s and idle-poll the ledger. True
signal: "queued *and* admittable under the rate/slot budget," not raw queue depth.

**6. A6's shard mechanism is named but not specified.** `hashtext(sample_id) % N` removes head-of-
line, but how do autoscaled workers (A1, dynamic count) map to shards N? Stored shard column vs
expression index? If N is fixed while worker count flexes, you need a claim scheme (randomized shard
offset per claim, or claim-any-shard). Tractable, unwritten.

**7. A11: within-shard durability lag (inherent, named).** Results commit only after the whole
shard's `inspect_eval` returns (runner.py:185–199), so a fast sample isn't durable until the slowest
agentic sample in its shard finishes, and a crash mid-shard re-executes the *entire* shard. The
amendment names shard-size-as-blast-radius as a tradeoff — honest; flagging it's a real coupling for
mixed fast/slow agentic shards.

## Bottom line
The one to treat as a blocker is **#1 (A3's ack-before-flip)** — a fresh correctness gap created by
an otherwise-good fix, sitting on the same "no completed sample lost" guarantee the whole ledger
design exists to protect. The rest are healthy v0.3 open items, not flaws. Good round of revision.

---
---

# Response to the second-round review

You were right on all seven, and #1 was a genuine correctness gap we introduced — fixed below. The
big new artifact is **`docs/SCHEDULER.md`**, which re-homes the pieces #4/#5 said were unhomed. All
changes are in DESIGN §15 (amended A-items now carry "2nd-round #N" tags) + the companion docs.

**#1 — A3 ack-before-flip (the blocker). FIXED.** You're exactly right: A3 turned an atomic
single-PG-txn commit into a cross-store write and we didn't re-specify durability ordering, opening a
lost-completed-sample path (`done` flipped before CH durably has the row). The commit protocol is now
specified (ORCH §5): **(1)** transcript → S3 (idempotent key), **(2)** async-insert → ClickHouse with
`wait_for_async_insert=1`, **block until durably acked**, **(3)** *only then* flip the ledger `done`.
**Invariant: `done` ⟹ result durable in CH.** Atomicity is replaced by ordering+idempotency (write
data durably, then commit the pointer); a crash before the flip re-executes the shard, and the
re-insert collapses via `ReplacingMergeTree((run_id,sample_id))`. Batching survives — `wait=1` still
coalesces server-side, one insert per shard, the ack amortizes over N samples.

**#2 — A4 killed live score metrics. FIXED.** Correct — "gateway + status" covered cost and progress
but not accuracy. Live run-level numbers now live on the **Postgres `runs` row**, written by the
Reconciler each tick: progress (ledger counts), **score** (a run-scoped CH aggregate
`avg(passed) WHERE run_id=X` — cheap, one partition), cost (gateway). Clients read live *and* final
from `runs` (`runs.status` distinguishes them); CH `run_summary` is the finalize analytics record.
(DESIGN §15 A4, ORCH §7, SCHEMA §2.)

**#3 — A10 T2 isolation downgrade. FIXED — and it simplifies A10.** You're right that reset-reuse and
snapshot-restore aren't interchangeable, and overlay rollback doesn't cover kernel state under gVisor,
surviving procs, or held connections. Tier-bound now: **T1** overlay reset-reuse (benign/narrow);
**T2 (default) = microVM snapshot-restore** — a *fresh* Firecracker VM per sample from a golden
snapshot, discarded after; **T3** single-use. Snapshot-restore keeps the scheduler out of the
per-sample path *and* gives fresh isolation, so there's **no "prove the reset is airtight" burden**.
Open-question #1 shifts from "secure reset" to "restore-pool sizing + golden-image management." Status
honestly marked **design-direction-unbuilt**, and the stale `builtins.py` per-sample-pod comment is
fixed. (DESIGN §15 A10, SANDBOXING §8/§11/§12.)

**#4 — A9 didn't re-home vLLM's global capacity. NAMED (deferred).** Re-homed in `docs/SCHEDULER.md`:
the global concurrency budget lives in an **in-path, capacity-aware inference router** (Envoy / vLLM
production-stack router — counts in-flight intrinsically, so no per-call Redis round-trip and no
Python-proxy ceiling) backed by **vLLM's own queue** for backpressure; **fairness** is enforced
upstream by the ledger **slot budget** (§13), not at the inference layer. Still future-phase (all
traffic is external today), but specified rather than waved at.

**#5 — A1's KEDA signal was naive. FIXED.** KEDA now scales on the **Scheduler's admittable-slot
count** (queued ∩ slot/rate budget), capped at `maxReplicas = global_slots / per_worker_concurrency`
(the gateway-rate-derived ceiling) — so workers never over-provision into 429-thrash. The Scheduler is
the single authority feeding *both* the claim layer and KEDA; full mechanism + the max-min fair-share
algorithm in `docs/SCHEDULER.md`.

**#6 — A6's shard mechanism. SIMPLIFIED AWAY for v1.** On reflection we judged the bucket scheme
heavier than the problem: claim QPS = throughput/batch_size is tiny for batched claims over
multi-second samples (~8/s at peak; <1 concurrent claim per run in realistic configs), and `not_before`
already handles the poison-sample head-of-line. So the **plain ordered claim is the v1 default**;
hash-bucketing is documented as a **purely-additive** optimization (`bucket` column + index + a
random-bucket filter) to add only if a batch≈1 / sub-second-sample / thousands-of-single-run-claimers
workload ever appears. (DESIGN §15 A6, SCHEDULER §6.)

**#7 — A11 within-shard durability lag. ADDRESSED via sharding policy.** A run is single-harness by
construction, so the residual is intra-agentic duration variance. Fix: **shard size defaults from
harness type** — large for fast/uniform (QA, amortize Inspect startup), **small (down to 1) for
high-variance/agentic** (no within-shard lag, crash blast-radius = one sample, and startup is
negligible against a multi-minute agentic sample anyway). Incremental per-sample commit is the
documented escape hatch for large agentic shards. (DESIGN §15 A11.)

Net: the blocker is closed with a specified commit protocol; #2/#3/#5 are resolved; #4 is named and
re-homed; #6 simplified; #7 handled by policy. New doc: `docs/SCHEDULER.md`. Thanks again — this
round caught a real correctness bug we'd have shipped.