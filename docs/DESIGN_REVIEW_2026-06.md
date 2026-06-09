# Design & codebase review — 2026-06-09

A full read of `eval_engine/` (all modules), the design docs (`DESIGN.md`, `ORCHESTRATION.md`,
`SCHEMA.md`, `SCHEDULER.md`, progress/refactoring/resilience docs), the test suite, the Postgres/
ClickHouse schemas, the K8s/KEDA manifests, and a skim of `frontend/`. Focus per the request:
**high-level design first**, then correctness, structure, and test coverage. Security was explicitly
out of scope. Low-level dead-code/naming findings already live in `docs/CODE_REVIEW.md` (2026-06-07)
and `docs/REFACTORING.md`; this review does not repeat them.

**Overall assessment.** This is an unusually disciplined codebase: small modules with explanatory
doc-comments that state *why*, a real layered test suite (unit / integration / e2e with genuine
Postgres concurrency tests), honest progress tracking, and a design-doc culture that records reversals.
The Inspect-native bet is executed consistently — harnesses/scorers are real Inspect objects, and the
plugin contract scaled from single-turn QA to agentic sandboxes to the multi-model Petri shape without
re-abstraction. The core mechanisms (ack-before-flip commit, `FOR UPDATE SKIP LOCKED` claim, lease
reclaim, leader election with pgbouncer-aware handover) are sound and tested.

The findings below cluster around one theme: **the implementation has quietly diverged from its own
specs in ways that matter at the design's stated scale** (~1B sample-evals/month, 12B retained rows).
None of it bites at today's prototype volumes — which is exactly why it's worth deciding now, while
the data is small and the fixes are cheap.

---

## 1. Design-level findings (highest leverage)

### D1. The ledger is not skinny — results flow through Postgres, contradicting the spec

`ORCHESTRATION.md` §4 is emphatic: *"Results never pass through widened ledger rows or a separate
loader"*; `SCHEMA.md` §1.6 specifies `sample_tasks` as status/lease/attempts/error only, *"skinny by
design."* The implemented ledger (`migrations/0001_initial_schema.sql:31`) carries the full result
payload — `passed, primary_score, scores JSONB, tokens_in/out, cost_usd, latency_ms, transcript_uri,
loaded` — and `commit_result` (`control.py:479`) writes results into it on every sample, alongside the
direct ClickHouse insert. A second analytics write path (`runner.batch_load` + the `loaded` flag +
`fetch_unloaded`) exists to drain ledger-held results into ClickHouse as a finalize safety sweep.

Why it matters at scale: the spec's stated reason for the skinny ledger was Postgres dead-tuple /
autovacuum churn at ~33M samples/day, each row written ≥3× (insert, claim, commit) plus lease renewals
— with JSONB `scores` adding TOAST churn. The widened ledger is *defensible* (it gives the live rollup
and budget gauge without touching ClickHouse, and a recovery path), but the trade-off was never
decided on the record — `ALTERNATIVES.md` records reversals, and this one isn't there.

**Suggestion:** make the decision explicit, one way or the other:
- *(a)* Keep the widened ledger: update ORCHESTRATION §4 / SCHEMA §1.6 to describe reality, record the
  reversal in ALTERNATIVES.md, and note the autovacuum tuning the design now needs at scale
  (`autovacuum_vacuum_scale_factor` on `sample_tasks`, fillfactor for HOT updates).
- *(b)* Re-skinny: commit results only to ClickHouse; serve the live rollup from CH with the full
  sort-key prefix as ORCHESTRATION §7 specifies, and the budget gauge from the gateway tally (the
  deferred A5). This is the spec'd design; it's more work and probably not justified until volume grows.

Option (a) + docs honesty is the pragmatic v1 answer; re-open (b) if Postgres bloat shows up in ops.

### D2. Per-run ClickHouse queries violate the schema's own access-pattern rule

`SCHEMA.md` §2 explains, at length, why per-run queries must pass the **full sort-key prefix**
(`eval_id, model_id, run_id`) — *"`run_id` is the 3rd ORDER BY column, so filtering on it alone can't
use the primary index → full current-month partition scan (~1B rows)"*. The implementation then does
exactly that: `analytics.run_summary` / `samples` / `by_category` (`analytics.py:199-223`) all filter
`WHERE run_id=` alone, **with `FINAL`**. These run on every finalize (`runner.py:492`), every run-page
results poll (`api.py:232`), and every training-monitor reconcile (`training.py:221`).

Today the table is small and nothing hurts. At the design's 1B rows/month, every run-page view becomes
a partition scan. The fix is nearly free *now*: the `runs` row already holds `eval_id` and `model_id`
for every run — thread them into the three query functions. (Alternative: a bloom-filter skip index on
`run_id`, or a projection — but passing the prefix is simpler and matches the documented design.)
Related: `compare_models_by_category()` is `FINAL` over the **whole table with no WHERE** — currently
dead code (see CODE_REVIEW.md §2), but it must not be wired into the Compare view in this form.

### D3. The `run_summary` finalize record doesn't exist in ClickHouse

SCHEMA §2 specifies a `run_summary` table written once at finalize (and DESIGN §8 calls it "the
finalize record"). Implemented finalize computes the headline numbers into the Postgres `runs` row
only; `analytics.run_summary()` is a query, not a table. Consequence at scale: any cross-run
comparison (the Compare view, regression dashboards) must re-aggregate `sample_results` per request
instead of scanning a tiny per-run rollup. Fine to defer — but it should be listed as an open gap in
`PROJECT_PROGRESS.md` (it currently isn't), because the Compare view will need it before 12B rows.

### D4. Ledger expansion happens in the API request path, not the orchestrator

ORCHESTRATION §3 gives the run FSM a `queued → expanding → running` lifecycle owned by the
orchestrator. Implemented: `runner.launch` (`runner.py:447`) — called synchronously inside
`POST /runs`, `POST /runs/{id}/rerun`, `POST /evals/{id}/launch`, and the training monitor's fan-out —
loads the whole dataset from object storage, parses it, and `executemany`-inserts one ledger row per
sample (`control.expand_tasks`). For a 100k-sample run that is a long, blocking HTTP request (dataset
download + 100k single-row INSERTs), and the run is fully expanded even if admission later holds it.
The API docstring says "expand ledger, return immediately" — true only for small datasets. The
`expanding` status still appears in the frontend's `RunDetail.status` union (`frontend/lib/api.ts:48`)
— a small fossil of the unimplemented state.

**Suggestion:** move expansion to the orchestrator's admit step (matches the spec, makes `POST /runs`
O(1), and keeps un-admitted runs out of the ledger — which also keeps the KEDA queue-depth signal
honest, since it counts `queued` rows of runs that may not be admitted for a while). If that's too
much motion for v1, at least batch the insert (`COPY` / multi-row VALUES) and document the deviation.

### D5. Workers re-load the full dataset from object storage on every poll

`worker._drain_run` (`worker.py:71-78`) fetches the spec **and loads the entire dataset** (object-store
read + JSONL parse + per-sample sandbox attach) *before* attempting a claim — and `main()` calls it for
**every running run on every ~1s poll**, even when there is nothing claimable (the common case for a
worker at the per-run `max_inflight` cap). With tens of concurrent runs this is a constant stream of
redundant GCS reads and parses on every worker; with 100k-sample datasets it's real memory and CPU.

**Suggestion:** claim first, load only when the claim returns ids; and cache datasets in-process keyed
by content hash (they are immutable by construction — the content-addressing already done in #9 makes
this cache trivially correct). This is the hottest easy win in the execute path.

### D6. A sample that crashes its worker is retried forever — no claim-side attempt cap

The retry cap lives only in `retry_or_fail` (`control.py:497`), which runs when the worker *survives*
to classify the failure. If executing a sample kills the worker process (OOM on a huge completion,
a segfault in a scorer dep, a node eviction mid-batch), the row stays `running` until lease expiry and
`claim_batch` (`control.py:421`) happily reclaims it — `attempts` increments without bound and the
sample crash-loops the whole fleet, forever, at full KEDA scale. Inspect's `fail_on_error=False`
catches most sample-level errors, so the trigger is narrow — but OOM is realistic, and the failure
mode is "every worker pod crash-restarts in rotation while KEDA holds them up," which is expensive
and won't self-heal.

Two adjacent gaps compound it:
- **Run-level fail-fast** (ORCHESTRATION §7: `failed_samples > max_failed` → run `failed`) is not
  implemented at all — a run with bad credentials burns `MAX_ATTEMPTS × total` doomed calls before
  "completing" with accuracy 0.
- **Deploy skew**: workers rehydrate specs and resolve plugins by `(type, version)` at claim time. A
  rollout that removes/renames a plugin version turns every already-queued run that references it into
  a fleet-wide worker crash-loop (`plugins.get` raises `KeyError` outside the per-sample error path).

**Suggestion:** (1) make `claim_batch` skip — or better, terminally fail — rows with
`attempts >= MAX_ATTEMPTS` (a one-line predicate plus an orchestrator sweep to mark them `failed`);
(2) add the spec'd run-level fail-fast threshold to the orchestrator reconcile; (3) wrap the worker's
per-run drain in a `try/except` that classifies *unexpected* exceptions as a `retry_or_fail` on the
claimed ids rather than crashing the pod.

### D7. Finalize is not crash-idempotent: prune happens before the terminal status flip

`runner.finalize` (`runner.py:486-499`) runs `archive_and_prune` **before** `finalize_run` flips the
run terminal. A crash between the two leaves a `running` run with an empty ledger — and the
orchestrator's finalize gate (`orchestrator.py:95`, `terminal >= total and total > 0`) can then never
pass, so the run is stuck `running` forever, occupying an admission slot and a worker-poll scan. The
orchestrator docstring claims "a crash mid-finalize just re-runs the (no-op-on-reentry) steps" — with
this ordering, that's not true. ORCHESTRATION §10 specifies the safe order (aggregate → archive →
prune → `completed` happens via the `finalizing` state, which the impl doesn't have).

**Suggestion:** flip status (or a `finalizing` marker) *before* pruning, or make the gate treat
"status `running` + ledger empty + `total > 0`" as finalizable by re-deriving counts from analytics.
Also wrap `cancel_run` and `archive_and_prune` in a single transaction — every `_conn().execute()` is
its own autocommit transaction (the `_ConnProxy` checks out a fresh pooled connection per statement),
so these multi-statement sequences have crash windows between every line; `control.connection()`
already exists as the right seam.

### D8. Leader-reaping assumes ticks stay short; slow ticks create a dual-leader window

`reap_stale_leader` terminates any advisory-lock holder idle >20s (`STALE_LEADER_SECONDS`). The
leader's lock connection is only touched by `leader_alive()` *between* ticks — so the safety property
is really "a tick never takes 20s." But tick work is unbounded: per-run ClickHouse summaries at
finalize (a `FINAL` scan each, see D2), the sandbox reaper shelling out to helm (120s timeout), and —
in the training monitor, which shares `run_as_leader` — dataset loads + full ledger expansion per
fanned-out checkpoint eval (D4). A slow tick gets the live leader reaped; both orchestrators then tick
concurrently until the old one notices. Most of that is idempotent by design, but a **double finalize
is not benign**: the second pass reads counts from an already-pruned ledger and overwrites the runs row
with `done=0, failed=0`.

**Suggestion:** heartbeat the leader connection from a small background thread (decoupling liveness
from tick duration), and/or make `finalize_run` refuse to regress counters (`done=GREATEST(done,%s)`,
or guard on `status='running'`). Bounding monitor fan-out work per tick helps independently.

### D9. Global rate limiting: the two progress notes are in tension — re-verify post-Sentinel

`PROJECT_PROGRESS.md` #1 (global rate limiting, load-tested as *shared via Redis* across gateway
replicas) and #16's Redis-HA caveat (*"the litellm router doesn't speak Sentinel … per-model rpm/tpm
caps fall back to per-replica in-memory limiting"*) can't both be true of the running system. Global
cross-worker rate limiting is one of the two things DESIGN §2 says *forces real infrastructure*, so
this deserves a definitive answer: after the move to the 3-pod Sentinel StatefulSet (where the `redis`
Service round-robins across master *and* read-only replicas — see `ops.probe_redis`'s own comment),
does the gateway still enforce a shared cap? If not, either point LiteLLM at a master-only endpoint
or accept and document per-replica limiting (cap ÷ N replicas).

### D10. Budget enforcement silently depends on a best-effort, unpinned price catalog

Worker-side cost = tokens × prices fetched live from `openrouter.ai` once per process
(`runner.py:150-166`); on any fetch failure, prices are empty and **all costs are $0 for that
process** — which doesn't just skew reporting, it **disables `budget_usd` enforcement entirely**
(`enforce_budget` compares committed cost to the cap). Prices also drift over time and across worker
processes, so a run's recorded cost isn't reproducible and isn't even internally consistent. The A5
deferral (gateway-canonical cost) is a reasonable call and well documented; the budget-cap dependency
on this soft path is the part that isn't.

**Suggestion:** ship a pinned price-table fallback (vendored JSON, refreshed deliberately) and record
the catalog version on the run; and when `budget_usd` is set but the catalog is empty, fail the launch
loudly rather than running uncapped.

---

## 2. Correctness findings (narrower, concrete)

- **C1. Cross-partition duplicate in analytics (narrow but permanent).** Both insert paths stamp
  `finished_at` at *write* time (`commit_batch` at commit, `batch_load` at load — `runner.py:430`).
  `ReplacingMergeTree` dedups **within a partition**, and the table partitions by
  `toYYYYMM(finished_at)`. A row re-inserted by the recovery sweep (or a re-execution) in a later
  *month* than the original lands in a different partition and never collapses — double-counted in
  every `FINAL` aggregate, forever. Persist the sample's true finish time in the ledger and reuse it
  on re-insert (the column exists in spirit — the ledger already carries the full result), or derive
  the partition from a stable per-sample timestamp.
- **C2. `latency_ms` is fiction.** Hardcoded `0` at `runner.py:398`, yet carried through ledger →
  ClickHouse → API → frontend as if measured (FR7 promises latency persistence). Populate it from
  Inspect's per-sample timing, or drop it from the API/UI until real — a column of plausible zeros is
  worse than no column.
- **C3. CLI `report` crashes on real transcripts.** `cli._print_report` (`cli.py:67-69`) reads
  `transcript_uri` with `Path(...).read_text()` + `json.loads` — but transcripts are now written
  zstd-compressed (`.json.zst`), so any local run with kept transcripts makes `eval-engine report`
  raise `UnicodeDecodeError`. Use `runner.get_transcript`. (The CLI unit tests mock
  `transcript_uri=""`, which is why this isn't caught.)
- **C4. Silent loss of the reproducibility pin at dataset registration.** `POST /datasets` wraps
  `snapshot()` in `except Exception: pass` (`api.py:274-279`) — an unreadable URI registers a dataset
  version with no `content_hash`/`snapshot_uri` and no signal to the caller. Return an explicit
  "unpinned" warning field (or 422 when the URI was supposed to be server-readable); log it either way.
- **C5. `update_live` can race a concurrent cancel.** The orchestrator computes the rollup, the user
  cancels (prunes the ledger, snapshots final counts), then the orchestrator's `update_live` lands —
  overwriting the cancelled run's counters with the pre-cancel snapshot (status stays `cancelled`;
  display-only). Guard the write with `WHERE status='running'`.
- **C6. `claim_batch` headroom check is racy.** Two concurrent claimers can both read headroom *H*
  and together exceed `max_inflight` (up to 2×). At ~8 claims/s this is acceptable — worth a comment
  in the SQL so nobody "discovers" it later as a bug.
- **C7. No transient/fatal error taxonomy.** ORCHESTRATION §6 distinguishes `RetryableError` from
  `FatalError`; `_settle_result` retries *every* errored sample. A deterministic failure (malformed
  sample, unsupported feature) burns all attempts at full backoff. Cheap version: classify on error
  message/type for the obvious deterministic cases.
- **C8. Terminally-failed samples keep no transcript.** `execute_batch` skips the transcript write for
  any errored sample (each retry "writes its own" — but the final, failed attempt writes none). The
  retention policy's own rationale is "failures are what you debug"; the `.eval` shard log is the only
  artifact left. Consider writing transcripts for terminal failures.
- **C9. Missing indexes for the polling paths.** `runs` has no index on `status` — `active_runs` is
  seq-scanned by every worker every poll and by KEDA every 20s, against a table that grows ~365k
  rows/year (partial index on `status IN ('queued','running')` fixes it). The claim's
  filter+`ORDER BY sample_id` can be served by one `(run_id, status, sample_id)` index instead of a
  sort over `ix_tasks_claim`.
- **C10. `expand_tasks` inserts row-at-a-time.** `executemany` of single-row INSERTs for up to 100k
  rows; use `COPY` or multi-row VALUES (compounds D4 while expansion lives in the request path).

---

## 3. Codebase structure

- **S1. `control.py` is a 950-line multi-concern module** — connection pool + retry layer, leader
  election, runs CRUD, the ledger, entity registry, audit, heartbeats/ops queries, and the entire
  training-monitor store. It's the module the previous review *skipped for size*, which is itself the
  signal. The section headers already mark the seams; splitting into a `control/` package
  (`pg.py` pool/retry/leader, `runs.py`, `ledger.py`, `registry.py`, `training.py`) is mechanical and
  would make the next correctness review of the claim/lease logic tractable.
- **S2. `runner.py` mixes five roles** — pricing, transcript persistence/retention, lane
  classification, the Inspect execute path, and run lifecycle (`launch`/`finalize`). The orchestrator,
  API, and training monitor import it for *non-execution* reasons (launch/finalize/budget), pulling
  the whole Inspect + sandbox import tree into control-plane processes. Natural extractions:
  `pricing.py`, `transcripts.py`, `lifecycle.py`. Lower priority than S1.
- **S3. The `_ConnProxy` type lie hides transaction boundaries.** `_conn()` returns a proxy that
  *quacks like* a `psycopg.Connection` (`# type: ignore`), but each `.execute()` is a separate pooled,
  autocommit statement. That subtlety is exactly what makes D7/C5 possible, and `con = _conn()`
  followed by several `con.execute(...)` lines *reads* like a transaction without being one. Rename
  the seam (`_exec(sql, params)`) so multi-statement flows are forced to choose `connection()`
  explicitly.
- **S4. The entity registry is stringly-typed and mutable where the docs say immutable.**
  Implementation is a generic `entities(kind,id,version,body JSONB)` table vs SCHEMA §1's relational
  design — fine for v1, but: `register_entity` *overwrites* an existing `(kind,id,version)`
  ("convenient in dev") while the models and API docstrings promise immutability; eval→dataset
  references are validated only at launch; and there are no `users`/`teams` tables at all behind the
  "tenancy-ready schema" claim (just a free-text `team` column on runs). Enforce version immutability
  outside dev (reject, don't upsert) — it's the cheap one with audit-grade value.
- **S5. Spec docs describe a system that no longer exists in places.** Beyond D1–D4:
  ORCHESTRATION §5's worker pseudocode uses a Redis stop-flag and `mark_failed` (neither implemented);
  §8/§9's budget/cancel semantics differ from the ledger-based implementation; SCHEMA's CH columns
  (Maps for scores/dimensions, `loaded_at` version, team/created_by) differ from the implemented
  String-JSON/`attempt` schema; CLAUDE.md references `control_pg.py` (now `control.py`). The docs are
  this project's best asset — drift erodes exactly the trust that makes them valuable. Suggest a
  one-line "implementation status" header per spec doc (FUTURE.md already models this), updated when
  an implementation knowingly deviates, with reversals recorded in ALTERNATIVES.md.
- **S6. Frontend API types are hand-maintained duplicates.** `frontend/lib/api.ts` re-declares every
  backend shape by hand; it already drifts (`RunDetail.status` includes `expanding`/`finalizing`,
  which the backend never emits). FastAPI gives OpenAPI for free — generating the client types
  (`openapi-typescript`) removes the whole drift class.

---

## 4. Test coverage

**Strong.** The layering (unit = no I/O, integration = real backends, e2e = full spine) is enforced by
directory + auto-marking; backends self-provision; CI runs integration + e2e-spine against real
Postgres/ClickHouse service containers with no secrets. The ledger suite is genuinely good — a
12-thread exactly-once test, lease reclaim/renew, retry backoff, budget, max-inflight, cancel-mid-run.
Past bugs got regression tests (CLI column drift, leader stall). Pure-logic modules
(`training_analysis`, `swebench` parsing, `ifeval`) test without backends.

Gaps, in priority order:

- **T1. The crash windows that the correctness story leans on are untested.** ORCHESTRATION §11's
  failure-mode table is the design's central claim, but there's no test that kills between commit
  steps: CH-insert-then-crash-before-flip (the ack-before-flip invariant is asserted only on the happy
  path), crash mid-finalize (would have caught D7), double-leader finalize (D8), batch_load recovery
  re-insert (would have caught C1). These are testable without fault injection — call the protocol
  steps out of order, run finalize twice, prune then re-gate.
- **T2. No lint or type gate.** No ruff/mypy config exists; CI runs tests only. Both prior review
  docs resorted to ad-hoc AST scans because no linter is installed. For a codebase this disciplined,
  `ruff` + a permissive `mypy` (the `_ConnProxy` ignore aside) is an hour of setup that replaces a
  whole review category.
- **T3. The worker/orchestrator *loops* are untested** — tests drive `_drain_run`/`tick` directly;
  `worker.main`'s poll/drain/heartbeat loop and the SIGTERM drain path are exercised only by a
  flag-setting unit test (`test_worker_shutdown.py` asserts `_STOP` flips). A loop-level test with a
  fake clock would cover the claim-starvation and shutdown sequencing.
- **T4. No scale smoke test.** The design hangs on "a plain claim is ample at ~8 claims/s" and
  expansion of 100k rows — one integration test that expands 100k tasks and times claims against a
  full ledger would pin both assumptions (and would have surfaced C9/C10).
- **T5. Petri e2e is key-gated (fine), but there's no offline contract test** for the
  `(solver, sandbox, model_roles)` / `(scorer, summarize)` plugin-shape handling in
  `runner._unpack_harness` + the summarizer path against a *mock* multi-role harness — that protocol
  is now load-bearing for every future eval shape. (`test_petri.py` covers `summarize_scores` itself.)
- **T6. Frontend tests are minimal** (a format helper + one UI test). Acceptable for a
  parity-tracked dashboard; the highest-value addition is a type-check (`tsc --noEmit`) in CI, which
  currently doesn't run the frontend at all.

---

## 5. Smaller observations (no action required, recorded for completeness)

- `plugins.Plugin.primary_metric` is registry metadata only — at runtime "primary" means "first scorer
  in the list" (`execute_batch`), and `passed` is a hardcoded `>= 0.5` threshold. Fine, but the
  catalog implies per-plugin semantics that nothing consumes; SCHEMA §4 lists this as an open
  question and the Petri summarizer already had to bypass it.
- Analytics `provider`/`model_id` come from `spec.model`, so a `checkpoint:` run records
  `provider=""`/`model_id="checkpoint:…"` while cost is priced against the resolved model
  (`exec_model`). Recording the resolved pair (it's known at commit time) would make cross-run model
  slices include checkpoint evals correctly.
- CH `scores` is a JSON string (spec says `Map(String, Float64)`) — Petri's ~38 judge dimensions are
  unqueryable without `JSONExtract` on 12B rows. Worth fixing while the table is small if dimension
  slicing is on the roadmap.
- No API pagination anywhere: `GET /runs` returns every run ever (≈365k/year at design rate);
  `GET /runs/{id}/results` returns all samples (100k rows of JSON for a full run); `list_entities`,
  heartbeats etc. are unbounded. Add `limit/offset` before the dashboard depends on response shape.
- `inspect-ai>=0.3.235` is unbounded above while `view_main.py` monkey-patches Inspect *private*
  modules (`_util.file`, `_view.fastapi_server`) and `runner` relies on `.eval` internals — any
  upstream release can break the image at build time. Pin an upper bound (or a lockfile for the
  Docker image); `test_view_main.py` covering the patch points is good and stays the canary.
- The two-lane admission counts a run with *zero* queued work (all inflight) as occupying its lane
  slot until finalize — fine at 50 max-running, just noting the semantics is "running runs," not
  "runs with claimable work."
- `worker._drain_run` drains runs strictly in `created_at` order each pass; per-run `max_inflight`
  prevents starvation, but a long-running early batch run gets first claim every loop. The SCHEDULER
  doc's fairness story holds only because of the caps — worth one sentence there.
- `datasets.snapshot` truncates SHA-256 to 16 hex chars (64 bits) for the content key — fine at this
  scale; just don't shorten it further.
- `cancel_run` is documented best-effort for in-flight batches, but the committed-after-cancel batch
  *does* land in analytics while the runs row's final counts exclude it — `/results` and the runs row
  can disagree slightly on a cancelled run. One sentence in the API docstring would set expectations.

---

## 6. Suggested priority order

| # | Item | Why first |
|---|---|---|
| 1 | D6 (claim-side attempt cap + fail-fast) | Only finding with an unbounded production failure mode (fleet crash-loop) |
| 2 | D7 (finalize ordering) + C5 guard | Cheap; removes "run stuck forever" states the orchestrator can't escape |
| 3 | D2 (sort-key prefix in per-run queries) | One-line-per-query now; a migration later |
| 4 | D9 (re-verify global rate limit post-Sentinel) | One of the two design-load-bearing mechanisms; currently unknown state |
| 5 | D5 (claim-before-load + dataset cache) | Hot-path waste, trivially cacheable |
| 6 | D10 (pinned prices / loud budget failure) | Budget caps currently fail open |
| 7 | D1/D3/D4/S5 (decide + re-align docs with impl) | Protects the project's main asset: trustworthy specs |
| 8 | C1–C4 (point fixes) | Small, well-bounded |
| 9 | T1, T2 (crash-window tests; ruff/mypy in CI) | Locks in everything above |
| 10 | S1/S2 (split `control.py`, then `runner.py`) | Do alongside, not before, the correctness items |
