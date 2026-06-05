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
  already pinned — so a run's inputs are now pinned per §14. **Partial:** the per-call provider
  version-fingerprint (e.g. OpenAI `system_fingerprint`) isn't recorded — Inspect's `ModelOutput`
  doesn't surface it cleanly; capturing it needs digging into the raw provider response (left as a
  documented follow-up). Tested (`test_distributed` asserts the pins are recorded).

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
  **Follow-up (not blocking):** the dashboard launch form still reads `/catalog`; a picker over
  registered evals/datasets/models — and launching a run *from* a registered eval (its dataset +
  default harness/scorers) — lands naturally with #10 (reproduce/launch).

- [x] **9. Dataset versioning — content-addressed snapshots.** §13. *Done (2026-06-05):*
  `datasets.snapshot(uri)` hashes a dataset's bytes and writes an **immutable, write-once** copy keyed
  by the hash (`gs://<bucket>/datasets/<hash>.jsonl` in-cluster, a local `.data/datasets/` dir in dev);
  `POST /datasets` calls it and pins `content_hash` + `snapshot_uri` on the registered `DatasetSpec`
  (the Postgres pointer from #8) — so a dataset version is reproducible by content, not by a mutable
  path. `datasets.load_jsonl` now reads a `gs://` snapshot too, so runs can execute against the
  pinned snapshot. Tested: content-addressed + idempotent + loads back to the same samples; endpoint
  enrichment verified. (Native GCS calls here are replaced by the S3-API abstraction in #14.)

- [ ] **10. Reproduce / "re-run".** FR10, §9.9. Clone a past RunSpec → a new Run with identical pinned
  inputs (endpoint + dashboard button).

- [ ] **11. Two-lane (interactive/batch) admission + per-run cap.** §8, `SCHEDULER.md`. A v1 item
  distinct from the deferred fair-share scheduler; today the orchestrator admits *all* queued runs.

- [ ] **12. `multiple_choice` harness.** §7. Today only `single_turn` + `agentic` (scorers: `includes`,
  `match`, `llm_judge`).

- [ ] **13. Audit log.** §8 (auth: audit), §13. None today.

### Tier 3 — production-shape (works, but not as designed)

- [ ] **14. S3-API storage abstraction (portability).** §4 mandates an `fsspec`/S3 abstraction — "no
  native GCS/Blob APIs in app code." Today `runner.py` uses `google.cloud.storage` directly and the
  viewer reads `gs://`.

- [ ] **15. Transcript retention: sample-by-default + zstd + tiering.** §8/§13. Today: plain-JSON,
  keep-all, no zstd, no stratified sampling, no storage-class tiering.

- [ ] **16. HA for stateful backends.** ClickHouse & Redis are single pods (acknowledged in
  `DEPLOYMENT.md`); CH insert is synchronous, not async-insert + durable ack.

- [ ] **17. Canonical per-`run_id` cost tally from the gateway.** §8 / DEPLOYMENT "A5". Today cost is the
  worker-side catalog price (equal in dollars, but not the canonical gateway tally).

- [ ] **18. gVisor (T2) isolation for agentic sandboxes.** `SANDBOXING.md`. Today runc
  (`CLUSTER_DEFAULT`); needs a GKE-Sandbox node pool (addable later w/o cluster recreation).

---

## Explicitly out of scope (deferred — see `docs/FUTURE.md`)

Not gaps; deferred behind a measured trigger: fair-share scheduler, vLLM direct/bypass path, microVM
snapshot-restore sandbox pool, Langfuse tracing, Superset BI, Postgres plugin catalog, hash-bucketed
claim, human-review UI, multi-tenancy enforcement, multi-container sandbox topologies, GPU-in-sandbox.
