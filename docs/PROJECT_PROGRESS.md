# Eval Engine — Project Progress & v1 Gap Backlog

> **Single source of truth for "what's built vs. what's left."** Pairs with `DESIGN.md` (the target)
> and `docs/DEPLOYMENT.md` (the GKE bring-up tracker). New session? **Start here**, pick the top
> unchecked item under [Open v1 gaps](#open-v1-gaps), build it, check it off.
>
> Scope rule: this backlog covers **v1 design scope only**. Anything in `docs/FUTURE.md` is
> **out of scope** (deferred behind a measured trigger) and is *not* listed here as a gap.
>
> Status legend: ☐ todo · ◐ in progress · ☑ done · ⊘ deferred (FUTURE.md)
>
> Last updated: 2026-06-05.

---

## What's standing (the spine)

Phases 0–3 are substantially up on GKE (`us-central1-a`, cluster `eval-engine`):

- **Kernel (Pure A):** Inspect AI; harnesses = Solvers, scorers = Scorers; `.eval` logs → GCS.
- **Control plane:** FastAPI (`api.py`), Postgres metadata + **ephemeral task ledger** with a real
  `FOR UPDATE SKIP LOCKED` claim, lease, crash-reclaim, archive+prune (`control_pg.py`).
- **Execution plane:** KEDA-autoscaled worker Deployment (claim→execute→commit→load), leader-elected
  orchestrator (PG advisory lock; admit + finalize).
- **Gateway:** LiteLLM fronts all model traffic (OpenRouter today), Redis wired for shared state.
- **Analytics:** ClickHouse `ReplacingMergeTree((eval_id,model_id,run_id,sample_id))`, monthly
  partitions, 12-month TTL; workers flatten each sample in.
- **Dashboard:** Next.js (runs list / detail / launch) + **embedded Inspect viewer** for transcripts.
- **Agentic:** k8s sandbox proven end-to-end (ephemeral per-sample pod, air-gap secret-read proof).
  Isolation = runc (`CLUSTER_DEFAULT`); prod gVisor is a cluster-rebuild (FUTURE.md §4).
- **Access:** Google OIDC (oauth2-proxy) ingress; Terraform + Helm IaC; pause/resume scripts.

Milestone-level detail (M0–M11) lives in `docs/DEPLOYMENT.md`.

---

## Open v1 gaps

The actionable backlog. Each item is **in `DESIGN.md` scope** and **not** implemented. Ordered by how
load-bearing the design says it is. Build top-down; update the box + a one-line note when you finish.

### Tier 1 — core mechanisms (highest leverage)

- [x] **1. Global rate limiting — actually enforced.** `DESIGN.md` §2 (one of "the two numbers that
  force real infrastructure"), FR6. *Done (2026-06-05):* per-model `rpm`/`tpm` in `30-litellm.yaml`
  (`usage-based-routing-v2` + `enable_pre_call_checks` + redis; `num_retries: 0` → fail-fast 429);
  litellm scaled to **2 replicas**. **Load-tested global enforcement:** filled an `rpm: 6` bucket on
  gateway replica A (5×200 then 429); replica B (different pod) then returned 429 for all requests —
  proving the cap is shared via redis (`global_router:<hash>:…:rpm` key), not per-replica. Caveat:
  fully-synchronized bursts can briefly overshoot the cap (read-then-increment race); converges to the
  cap over the window — acceptable for provider-quota protection.

- [x] **2. Per-sample retry with backoff to N.** FR5, §9.5. *Done (2026-06-05):* the claim now
  respects `not_before` (both backends); `control.retry_or_fail` re-queues a transient failure with
  exponential `not_before` backoff up to `MAX_ATTEMPTS` (env `EVAL_ENGINE_MAX_ATTEMPTS`, default 3),
  then terminal `failed`. `runner._execute_batch` captures Inspect's per-sample `.error` (an execution
  error, distinct from a low score) and routes it via `_settle_result` to commit-or-retry; the
  single-process `runner.execute` waits out backoffs before finalize (distributed path uses the
  orchestrator's `queued==0` finalize gate). Tested on SQLite + Postgres (`test_retry_backoff`):
  re-queue → `not_before` blocks the claim → attempt-cap → terminal; `exactly-once` unaffected.

- [x] **3. Budget caps + `BudgetExceeded` terminal class.** FR6, §8. *Done (2026-06-05):*
  `RunSpec.budget_usd`. Control-plane enforcement (the canonical gateway per-call reject is the
  deferred "A5"): `control.run_cost` gauges committed cost; when it reaches the budget, the
  orchestrator (and the single-process `runner.execute`) call `control.budget_stop`, which converts
  still-`queued` tasks to a **distinct terminal `budget_skipped`** status (error_type
  `budget_exceeded`) — *not* `failed`, so it neither inflates `failed_samples` nor burns retries;
  in-flight samples finish. The finalize gate counts `budget_skipped` as terminal, `archive_and_prune`
  records it, and a budget-capped run finalizes with status `budget_exceeded`. Tested on SQLite +
  Postgres (`test_budget_stop`); e2e on the cluster (run capped mid-flight, remaining samples skipped).

- [x] **4. Epochs + confidence intervals.** §14, FR8. *Done (2026-06-05):* `RunSpec.epochs` +
  `temperature`/`seed`; `runner._execute_batch` passes `epochs` to `inspect_eval` (Inspect repeats
  each sample N× and reduces to one per-sample row — ledger/analytics unchanged) and threads
  temperature/seed into the `GenerateConfig`. `runner.wilson_ci` computes a 95% Wilson score interval
  on the pass rate (stable at small n / extreme rates, never escapes [0,1]); surfaced as
  `summary.accuracy_ci` in the API and shown under the accuracy metric in the dashboard
  ("95% CI lo–hi%"). Tested (`test_epochs_and_ci`): epochs reduce 3×-repeated samples to 3 rows;
  CI(50/100)=[0.404,0.596]. Caveat: per-sample cost reflects the reduced sample, so epoch cost is
  approximate (the canonical gateway tally is the deferred "A5").

- [x] **5. Commit protocol: ack-before-flip.** §8, `ORCHESTRATION.md` §5. *Done (2026-06-05):* new
  `runner._commit_batch` (used by `worker._drain_run` + `runner.execute`) inserts each clean result to
  ClickHouse **first** (synchronous = durable ack), **then** flips the ledger row to `done` (+`loaded`).
  Invariant: `done ⟹ result durable in analytics`. A crash after the insert but before the flip leaves
  the row `running` → re-claimed → re-inserted with a higher `attempt` (ReplacingMergeTree version) so
  the retry wins; `done` rows are never missing from CH. `control.attempts_for` reads the ledger
  version before the flip; transcript write already precedes both. Tested: distributed run asserts
  `fetch_unloaded == []` after drain (no done-but-unloaded row) and analytics fully populated
  pre-finalize; both backends green.

- [x] **6. Live metrics on the `runs` row.** §8 "Live metrics". *Done (2026-06-05):* each tick the
  orchestrator computes `control.live_rollup` (one-pass done/failed/passed/cost over committed ledger
  rows) and `update_live` writes done + failed + live accuracy + `cost_usd` onto the `runs` row — so
  clients read live progress/score/cost from one authoritative place (the runs list now shows live
  accuracy mid-run, not just at finalize). Added a `cost_usd` column (PG `ALTER … IF NOT EXISTS`;
  SQLite guarded PRAGMA migration); `finalize_run` persists final cost. Also fixed a latent bug:
  `get_run` used `SELECT *` mapped positionally, misaligning `created_at`/`finished_at` past the
  unmapped `spec_json`/`created_by` — now an explicit `RUN_COLS` (kept in sync with `api.get_run`),
  which also surfaces `cost_usd` + `created_by`. Tested (`test_live_rollup`, both backends).

- [x] **7. RunSpec reproducibility fields.** §7, §14. *Done (2026-06-05):* `RunSpec.eval_version`
  (pin eval@version, recorded on the run instead of the old hardcoded `1`), `RunSpec.team` (ownership,
  tenancy-ready; enforcement deferred), and a **worker image/code pin** — the Dockerfile stamps the
  build's git SHA (`ARG GIT_SHA` → `ENV EVAL_ENGINE_IMAGE_DIGEST`, built with
  `--build-arg GIT_SHA=$(git rev-parse --short HEAD)`), which `runner` records as `image_digest` on
  every run. New `team`/`image_digest` columns (PG + SQLite migrations); surfaced in `GET /runs/{id}`.
  `sampling{epochs,temperature,seed}` (#4) and `budget` (#3) already landed, and `dataset_hash` was
  already pinned — so a run's inputs are now pinned per §14. Tested (`test_distributed` asserts the
  pins are recorded). **Provider version-fingerprint — done (2026-06-05):** a `provider_fingerprint`
  column on `runs` records the **resolved model the provider echoes back** (`ModelOutput.model`) plus
  its **`system_fingerprint`** when exposed (`model@fp`; some openai/groq models surface it,
  mock/OpenRouter often don't). `runner._execute_batch` captures it per sample; `control.set_fingerprint`
  pins the first one seen (NULL-guarded, first-writer-wins); surfaced in `GET /runs/{id}`. Tested
  (`test_provider_fingerprint_pinned`) + in-cluster.

---

> **Tier 1 (core mechanisms) complete.** Next up: Tier 2 platform surface (#8–#13).

### Tier 2 — platform surface (FR1–3, FR10)

- [x] **8. Dataset / Eval / Model registration + CRUD.** FR1–3. *Done (2026-06-05):* typed entity
  specs (`DatasetSpec` / `EvalSpec` / `ModelSpec`) + a versioned registry (`control.register_entity`
  / `list_entities` / `get_entity`, backed by a generic `entities(kind,id,version,body)` table on
  PG + SQLite). API: `POST/GET /datasets`, `/evals`, `/models` (+ `GET /{id}` → latest version);
  versions are immutable (re-register a new version, no PUT/DELETE — the content-addressed stance of
  §13/§14), and `POST /evals` validates the bundled harness/scorers exist (422 otherwise). Tested:
  `test_registry` (both backends) + a FastAPI TestClient smoke (register/list/get/validation/404).
  **Launch-from-registered-eval — done (2026-06-05):** `POST /evals/{id}/launch` resolves the eval's
  dataset (its pinned content-addressed **snapshot**) + default harness/scorers server-side, applies
  the caller's model + run knobs, and launches (audited `run.launch_from_eval`). The dashboard launch
  drawer gained an **ad-hoc | from-registered-eval** toggle: the eval mode shows a versioned-eval
  picker (with its resolved dataset/harness/scorers) and launches via the new endpoint. Tested
  (`test_launch_from_registered_eval`, `test_launch_from_eval_errors` — 404/422) + in-cluster.

- [x] **9. Dataset versioning — content-addressed snapshots.** §13. *Done (2026-06-05):*
  `datasets.snapshot(uri)` hashes a dataset's bytes and writes an **immutable, write-once** copy keyed
  by the hash (`gs://<bucket>/datasets/<hash>.jsonl` in-cluster, a local `.data/datasets/` dir in dev);
  `POST /datasets` calls it and pins `content_hash` + `snapshot_uri` on the registered `DatasetSpec`
  (the Postgres pointer from #8) — so a dataset version is reproducible by content, not by a mutable
  path. `datasets.load_jsonl` now reads a `gs://` snapshot too, so runs can execute against the
  pinned snapshot. Tested: content-addressed + idempotent + loads back to the same samples; endpoint
  enrichment verified. (Native GCS calls here are replaced by the S3-API abstraction in #14.)

- [x] **10. Reproduce / "re-run".** FR10, §9.9. *Done (2026-06-05):* `POST /runs/{id}/rerun` clones the
  stored RunSpec → a new Run with identical pinned inputs (eval@version, dataset content hash, model +
  params + seed, epochs, budget, image digest from #3/#4/#7/#9), returning `{run_id, rerun_of}`. A
  `↻ re-run` button on the dashboard run-detail header fires it and navigates to the clone. Tested
  (TestClient: clone preserves `eval_version`, 404 on missing run).

- [x] **11. Two-lane (interactive/batch) admission + per-run cap.** §8, `SCHEDULER.md`. *Done
  (2026-06-05):* **(a)** the load-bearing per-run cap — `claim_batch` now claims only
  `LEAST(batch, max_inflight − live_running)` (expired leases excluded, so reclaim still works), so a
  big run can't eat the cluster and workers flow to runs with headroom. **(b)** Runs are classified at
  launch (`runner._classify`): a `limit` or ≤`INTERACTIVE_MAX_SAMPLES` ⇒ `interactive` (small
  `max_inflight`=5, many iterators progress), else `batch` (`max_inflight`=50); explicit `RunSpec.lane`
  overrides. **(c)** The orchestrator's `_admit` does two-lane admission: a global cap on running runs
  + a reserved interactive slice that batch borrows only when there's no interactive demand and yields
  (by attrition, never mid-run) when there is. New `lane`/`max_inflight` columns. Tested on SQLite +
  Postgres (`test_max_inflight_cap`, `test_lane_classification`); exactly-once (12 workers) unaffected.

- [x] **12. `multiple_choice` harness.** §7. *Done (2026-06-05):* `multiple_choice` harness (Inspect's
  MC solver, optional `cot`) + a `choice` scorer (grades the selected letter against the target);
  `datasets.load_jsonl` now reads a `choices` list into the Inspect `Sample`. Example
  `examples/mcq.{jsonl,yaml}`. Catalog now spans the QA, agentic, and multiple-choice eval shapes
  (harnesses: `single_turn`, `multiple_choice`, `agentic`; scorers: `includes`, `match`, `choice`,
  `llm_judge`). Tested (`test_multiple_choice_plugins`).

- [x] **13. Audit log.** §8 (auth: audit), §13. *Done (2026-06-05):* append-only `audit_log` table
  (PG + SQLite); `control.audit(actor, action, target, detail)` records every mutating action and
  `list_audit` reads it newest-first. Wired into `run.launch`, `run.rerun`, and
  `{dataset,eval,model}.register` (actor = the OIDC `X-Auth-Request-Email`); exposed at `GET /audit`.
  Tested (`test_audit`, both backends + TestClient).

> **Tier 2 (platform surface) complete.** Remaining: Tier 3 production-shape (#14–#18).

### Tier 3 — production-shape (works, but not as designed)

- [x] **14. S3-API storage abstraction (portability).** §4 mandates an `fsspec`/S3 abstraction — "no
  native GCS/Blob APIs in app code." *Done (2026-06-05):* new `eval_engine/storage.py` — a single
  fsspec interface (`read_bytes`/`write_bytes`/`read_text`/`exists`) where the **URI scheme selects
  the driver** (`gcsfs` for `gs://`, `s3fs` for `s3://`, local path for dev), so the object store is
  swappable with no code change. `runner.py` (transcripts) and `datasets.py` (content-addressed
  snapshots + load) now go through it; the native `google-cloud-storage` SDK is **dropped entirely**
  (`pyproject` `[gcs]` = just `gcsfs`, the fsspec driver; `fsspec` is a base dep). The Inspect viewer
  (`view_main.py`) already uses Inspect's own fsspec layer — no native SDK there. Tested
  (`test_storage.py`, local fs) + in-cluster (a real gateway run writes/reads its transcript over
  `gs://` and the dataset snapshot lands in `gs://…/datasets/`).

- [x] **15. Transcript retention: sample-by-default + zstd + tiering.** §8/§13. *Done (2026-06-05):*
  **(zstd)** transcripts are written zstd-compressed as `…/<sample>.json.zst`; `get_transcript`
  transparently decompresses (legacy `.json` still served). **(stratified sampling)**
  `runner._keep_transcript` keeps **all failing** samples (what you debug) plus a **deterministic
  fraction of passes** (`_hash01(sample_id) < rate`, stable in/out); rate from
  `RunSpec.transcript_sample_rate` → else `EVAL_ENGINE_TRANSCRIPT_SAMPLE_RATE` env (unset ⇒ keep all,
  so dev/tests are unchanged; the worker Deployment sets **0.25** = sample-by-default). **(tiering)**
  a GCS lifecycle on the bucket cools `runs/` + `eval-logs/` to **NEARLINE at 30d, COLDLINE at 90d**
  (added to `deploy/terraform/main.tf`, applied + verified live). Tested (`test_transcripts.py`:
  retention policy + zstd round-trip) + in-cluster (`.json.zst` written to `gs://` and read back;
  `rate=0` keeps only failures).

- [◐] **16. HA for stateful backends.** ClickHouse & Redis are single pods (acknowledged in
  `DEPLOYMENT.md`); CH insert is synchronous, not async-insert + durable ack. *Async-insert + durable
  ack done (2026-06-05):* `analytics.insert` now uses `async_insert=1, wait_for_async_insert=1` — the
  server batches concurrent worker inserts as one part (no write-amplification under load) while the
  call still blocks until durable, preserving the ack-before-flip invariant (tested; verified
  in-cluster). *Remaining (pod-replica HA):* replicated ClickHouse + Redis (Sentinel) need a
  **multi-node** cluster (anti-affinity across nodes) — a deliberate departure from the cost-minimal
  single-node design (Redis state is intentionally soft/rebuildable). Gated behind a cost decision.

- [⊘] **17. Canonical per-`run_id` cost tally from the gateway.** §8 / DEPLOYMENT "A5". Today cost is the
  worker-side catalog price (equal in dollars, but not the canonical gateway tally). *Decision
  (2026-06-05): keep the catalog price.* The gateway fronts OpenRouter at catalog rate, so the
  worker-side price **equals** the gateway spend in dollars today; the canonical form would need a
  LiteLLM spend-DB + per-call `run_id` tagging threaded through Inspect (not cleanly exposed) for the
  same figure — deferred as uncertain-payoff (DEPLOYMENT.md "Canonical A5"). Re-open if a provider is
  fronted whose gateway price diverges from the catalog (e.g. negotiated/volume pricing).

- [x] **18. gVisor (T2) isolation for agentic sandboxes.** `SANDBOXING.md`. *Done (2026-06-05):* added
  a **GKE-Sandbox (gVisor) node pool** (`deploy/terraform` — autoscales 0..2, ~$0 idle; `sandbox_config`
  is `google-beta`-only in provider v6, so that resource uses the `google-beta` provider, and the live
  pool was bootstrapped via `gcloud` + `terraform import`ed). Flipped the sandbox `runtimeClassName`
  from `CLUSTER_DEFAULT` (runc) → **`gvisor`** in `deploy/sandbox/k8s-agent-env-values.yaml`; GKE
  injects the matching toleration/nodeSelector so per-sample sandbox pods land on the gVisor pool.
  Verified in-cluster: an agentic run's sandbox pod showed `runtimeClassName=gvisor` scheduled onto a
  `gke-eval-engine-sandbox-…` node (the pool auto-provisioned 0→1), and the in-pod `bash` read the
  sandbox-only secret (proof the tool ran inside the gVisor sandbox).

---

## Bugs found during the backlog work (not in the original gap analysis)

- [x] **B1. Orchestrator leader-election stalls on every rollout (pooled Postgres).** *Fixed
  (2026-06-05):* two-part handover. (1) A SIGTERM handler (`_graceful_shutdown` → `release_leader`)
  `pg_advisory_unlock`s before the pod exits — so a rollout hands leadership over in ~1s (merely
  closing the client conn doesn't release the lock through pgbouncer; the explicit unlock does).
  (2) For an *ungraceful* death (SIGKILL/OOM/node loss) where SIGTERM never ran, the standby loop
  `reap_stale_leader`s — terminates the lock holder once it's been idle past `STALE_LEADER_SECONDS`
  (20s; a live leader refreshes its lock connection every 2s tick via `leader_alive`, so idle>20s is a
  safe "crashed" signal). Both no-ops on the single-process SQLite backend.

## Explicitly out of scope (deferred — see `docs/FUTURE.md`)

Not gaps; deferred behind a measured trigger: fair-share scheduler, vLLM direct/bypass path, microVM
snapshot-restore sandbox pool, Langfuse tracing, Superset BI, Postgres plugin catalog, hash-bucketed
claim, human-review UI, multi-tenancy enforcement, multi-container sandbox topologies, GPU-in-sandbox.
