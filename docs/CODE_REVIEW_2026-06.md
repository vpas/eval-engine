# Eval Engine — Codebase Review (2026-06)

> A point-in-time review covering general design, code structure, correctness, security, test
> coverage, and deployment. Findings are advisory — nothing here has been changed in the code.
> Each item lists a severity, a location, and a suggested direction. Severities are **High**
> (correctness/security risk worth fixing soon), **Medium** (real but bounded), **Low** (polish).
>
> Scope note: this is a v1 prototype that explicitly defers a lot behind triggers (`docs/FUTURE.md`).
> Several gaps below are *known* deferrals; they're listed for completeness with that flag.

## Overall assessment

This is a strong, coherent codebase. The architecture is disciplined (Inspect-native kernel,
Postgres ledger as the single coordinator, ClickHouse as a derived projection), the module
boundaries are clean, and the hardest part — the distributed claim/lease/retry/finalize protocol —
is both well-implemented and genuinely well-tested under real concurrency. Documentation is unusually
good: nearly every non-obvious decision carries an inline rationale tied to a design doc.

The weak spots are the edges, not the core: a couple of correctness/security issues in the HTTP
surface, container/deploy hardening that hasn't caught up to the application maturity, and some
duplication in the frontend. None of these undermine the design; they're the normal "productionize
it" backlog.

Highlights worth preserving:
- The ledger claim (`control.claim_batch`) is a real `FOR UPDATE SKIP LOCKED` with a per-run
  `max_inflight` cap folded into the `LIMIT`, proven concurrent-safe by `test_exactly_once`
  (2000 samples × 12 workers, zero double-claims).
- The ack-before-flip commit (`runner.commit_batch`) gives a clean crash-safety invariant
  (`done` ⟹ durable in analytics), and it's backed by ReplacingMergeTree dedup so retries are safe.
- Connection resilience in both `control._run` and `analytics._run` is thoughtfully reasoned about
  (idempotency of each retried statement is argued explicitly).

---

## High severity

### H1. `/transcript` endpoint allows arbitrary local file reads (path traversal / LFI)
**`eval_engine/runner.py:294` (`get_transcript`), exposed at `api.py:191` (`GET /transcript?uri=…`).**
The endpoint passes a caller-controlled `uri` straight to `storage.read_bytes`. The only guard is for
`gs://` URIs (bucket allow-list); any non-`gs://` value skips that check and is read from the local
filesystem. In dev (`INLINE_EXEC`, no `GCS_BUCKET`) `GET /transcript?uri=/etc/passwd` returns the
file. Even in-cluster, a non-`gs://` path bypasses the bucket check entirely.
**Fix:** reject any `uri` that isn't an expected transcript location — require the `gs://<our-bucket>/`
prefix in cluster mode, and in dev confine to the `TRANSCRIPTS` root via a realpath prefix check
(resolve symlinks, then assert the resolved path is under the transcript directory). Don't read
arbitrary schemes/paths from a query parameter.

### H2. No authentication/authorization enforcement on the API
**`eval_engine/api.py` throughout.** `auth_email` only *reads* an identity header from the OIDC proxy
for attribution; no endpoint enforces it. Anyone who can reach the API (in dev: anyone; in cluster:
anyone past the ingress) can launch, cancel, re-run, or register entities, attributed to whatever the
proxy forwards. `team`/tenancy is carried but never checked (acknowledged as deferred in
`models.py:127`). This is partly by-design for v1, but the *cancel/rerun/launch* mutations have no
ownership check at all — a single compromised or misconfigured path is a full control-plane.
**Fix (at least):** treat absence of an authenticated email as 401 on mutating routes when running
in cluster mode (gate on a config flag so dev/port-forward stays open), and add an ownership check on
`cancel`/`rerun` against `created_by`/`team`. Track the rest under the deferred tenancy item.

### H3. Containers run as root with no pod `securityContext`
**`deploy/Dockerfile`, `frontend/Dockerfile`, and `deploy/k8s/{40-control-plane,50-worker,80-frontend,30-litellm}.yaml`.**
Neither image sets a `USER`, and the workload manifests omit `runAsNonRoot`,
`allowPrivilegeEscalation: false`, `readOnlyRootFilesystem`, and `capabilities.drop: [ALL]`. For a
platform whose whole job is running untrusted model output (and, in the agentic path, executing
model-authored code), root containers are a meaningfully larger blast radius than necessary.
**Fix:** add a non-root `USER` to both Dockerfiles and a pod-level `securityContext` to every
workload. (The agentic *sandbox* pods are separately isolated — this is about the app pods.)

### H4. Mutable `:latest` image tags defeat the reproducibility pin
**`deploy/k8s/{20-schema-init-job,80-frontend,85-inspect-view}.yaml` use `:latest` with
`imagePullPolicy: Always`.** The app records `IMAGE_DIGEST` per run as a repro pin (`runner.py:50`),
but deploying `:latest` means "which code ran this" is not actually pinned at the cluster level, and
two pods in one rollout can run different code. LiteLLM is correctly digest-pinned — the inconsistency
is the tell.
**Fix:** tag images with the Git SHA (the Dockerfile already takes `GIT_SHA`) and reference that tag
in the manifests; reserve `:latest`/`Always` for hotfix workflows.

---

## Medium severity

### M1. No Python dependency lockfile
**`pyproject.toml`.** Dependencies are version-ranged with no `requirements.lock`/`poetry.lock`, so
image rebuilds are non-deterministic and the repro pin (H4) is undermined a second way — the same
Git SHA can resolve different transitive deps. **Fix:** `pip-compile`/`uv pip compile` a hashed lock
and install with `--require-hashes` in the image build.

### M2. `ops._throughput` mutates module-global state without locking
**`eval_engine/ops.py:411-420`.** The rolling-throughput cache (`_thr`) is read-modify-written with no
synchronization. With >1 API worker/thread this is a benign-but-wrong race (occasional bogus rate, or
a negative clamped to 0). The comment says "single api replica" but nothing enforces it.
**Fix:** guard with a lock, or compute throughput from a timestamped DB query instead of process state.

### M3. `commit_result` / `mark_loaded` aren't guarded by `claimed_by`
**`eval_engine/control.py:479-487, 567-573`.** `claim_batch`/`renew_lease`/`retry_or_fail` all guard
on `claimed_by` + `status='running'`, but `commit_result` flips a row to `done` keyed only on
`(run_id, sample_id)`. If a lease expired and another worker re-claimed the sample, the original
(slow) worker can still commit its stale result over the new owner's row. The ack-before-flip +
ReplacingMergeTree(attempt) design makes the *analytics* outcome converge, but the *ledger* row can
flip `done` from a worker that no longer owns it.
**Fix:** add `AND claimed_by=%s AND status='running'` to `commit_result` (and have the caller treat a
0-rowcount as "lost the lease, drop the result"). Low real-world probability, but it's the one place
the lease invariant isn't enforced.

### M4. Frontend: duplicated constants/utilities across pages
**`frontend/app/ops/page.tsx` vs `frontend/app/system/page.tsx`.** `HEALTH`, `PULSE`, `METRIC_LABEL`,
`fmtDur`, `fmtMetric` are defined separately in both — and the `HEALTH` schemas have already drifted
(`ops` has a `b`/border field the `system` copy lacks). This is a live inconsistency, not a
hypothetical. **Fix:** extract to `frontend/lib/constants.ts` and import in both.

### M5. Frontend: oversized page components
**`frontend/app/runs/[id]/page.tsx` (~507 lines), `frontend/app/training/[id]/page.tsx` (~420),
`frontend/app/compare/page.tsx` (~310).** Each bundles 5–8 nested subcomponents (header, spec panel,
live samples, drawers, charts) in one file. They're already factored into functions, so the lift is
small — split into per-component files for testability. **Fix:** mechanical extraction.

### M6. No NetworkPolicies; secrets unencrypted at rest; local Terraform state
**`deploy/k8s/` (no `NetworkPolicy`), `deploy/secrets.sh`, `deploy/terraform/` (no `backend` block).**
Default-allow pod networking, K8s Secrets relying on etcd defaults, and local TF state (which contains
the PG DSN and IAM material) are the standard "POC → prod" hardening items. Cilium is already present
(the sandbox ns uses `CiliumNetworkPolicy`), so policies are low-friction to add. **Fix:** deny-all +
explicit allows per workload; GCS backend for TF state with a KMS key; move toward Secret Manager +
Workload Identity (already flagged in `RESILIENCE.md`).

### M7. CLI crashes on a corrupt/missing transcript
**`eval_engine/cli.py:67-68`.** `_print_report` does `json.loads(Path(uri).read_text())` with no
guard; one malformed transcript aborts the whole report. **Fix:** try/except per row, warn and skip.

### M8. Frontend training-detail polling references run status outside its dep array
**`frontend/app/training/[id]/page.tsx:38`.** `refetchInterval` reads `run?.status` via closure; if
the run goes terminal mid-poll the interval may not stop cleanly. **Fix:** derive the interval from
React Query's query state, or include the status in the controlling value.

---

## Low severity

### L1. `update_training_run` builds SQL via f-string interpolation of a SET-clause list
**`eval_engine/control.py:780-788`.** The interpolated fragments are all hardcoded literals today, so
it's safe — but it's the one place the otherwise-100%-parameterized codebase concatenates SQL. Keep
it from becoming a foothold: assemble from a fixed whitelist mapping, never from caller strings.

### L2. `db_migrate.apply` computes `to_apply` twice
**`eval_engine/db_migrate.py:44-46`.** `pending` is computed, consumed by `len(list(...))`, then
`to_apply` is called again for the actual apply. Harmless (re-iterable) but wasteful and slightly
confusing. **Fix:** materialize once and reuse.

### L3. Frontend: truncation is silent
**`runs/[id]/page.tsx:238` (500 live samples), `compare/page.tsx:76,283` (40 runs / 200 rows).**
Results are sliced with no UI indication. **Fix:** show a "showing N of M" affordance.

### L4. Frontend: non-null assertions and `any` in compare/launch
**`compare/page.tsx:164` (`A.res!`/`B.res!`), `launch.tsx:207` (`s: any`).** Runtime-guarded today but
less self-documenting; tighten the types on entity bodies.

### L5. Frontend: clickable `<div>`s without semantics
**`training/[id]/page.tsx:255`, `system/page.tsx:208`.** Add `role="button"`/`tabindex`/`aria-label`,
and wire the appbar `/`-to-search hint to a real handler.

### L6. `swebench.persample_sandbox` check-then-write is non-atomic
**`eval_engine/swebench.py:57-77`.** TOCTOU between `exists()` and write; harmless (idempotent
overwrite) but worth an atomic write-if-absent if it ever matters.

---

## Test coverage

Strong. ~205 tests across unit/integration/e2e; **every** production module has direct or indirect
coverage, and the load-bearing protocols are tested against *real* Postgres and ClickHouse (no
sqlite/in-memory stand-ins):

- **Exactly-once claiming** under true parallelism (`tests/integration/test_ledger.py::test_exactly_once`).
- **Lease reclaim + renew** (`test_lease_reclaim`, `test_renew_lease_prevents_reclaim`).
- **Retry backoff, budget stop, live rollup, max-inflight cap** — each its own integration test.
- **Full distributed spine** launch→admit→drain→finalize, asserting the ack-before-flip invariant and
  ledger pruning (`tests/e2e/test_spine.py`).
- **Leader election** against real advisory locks; **admission lanes**; the **API surface**.

Fixtures are well-designed (real backends auto-provisioned, schema once per session, TRUNCATE between
tests). CI (`.github/workflows/ci.yml`) runs everything except the docker-in-docker agentic sandbox
test, with Postgres + ClickHouse service containers.

Gaps worth a follow-up (all low/medium, acceptable for v1):
- Concurrent `finalize` idempotency isn't unit-tested in isolation (only exercised via e2e).
- No test for a ClickHouse insert failing mid-batch (partial-load recovery).
- No test for worker dying *after* claim, *before* commit, beyond the lease-reclaim path.
- No test asserting `load_jsonl` behavior on corrupt JSONL (it currently raises — worth pinning).
- The M3 stale-commit race above has no test (because the guard isn't there yet).
- CI has no image vulnerability scan, manifest lint, or secret-detection step.

---

## Suggested priority order

1. **H1** (transcript LFI) and **M3** (stale-commit guard) — small, concrete correctness/security fixes.
2. **H3 / H4 / M1** — container `USER` + `securityContext`, SHA-pinned images, dependency lock. One
   coherent "make the image production-grade" pass.
3. **H2** — gate mutating routes on authenticated identity in cluster mode + ownership on cancel/rerun.
4. **M6** — NetworkPolicies, TF state backend, secret-at-rest. The standard prod-hardening sweep.
5. **M4 / M5 / M2 / M7 / M8** — duplication, oversized components, the throughput race, CLI robustness.
6. **L1–L6** — polish as you touch the files.
