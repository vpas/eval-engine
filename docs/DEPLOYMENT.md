# Eval Engine — GCP Deployment & Implementation Tracker

> **Living progress doc.** Goal: stand up the **current v1 design** (`DESIGN.md`) on GKE and run a
> real eval **end-to-end**, spending as little as possible. First target is a **QA-only e2e** (no
> agentic sandbox — that's `docs/FUTURE.md` §4). Update the status boxes as we go.
>
> Status legend: ☐ todo · ◐ in progress · ☑ done · ⊘ deferred.

Last updated: 2026-06-04.

---

## 0. Scope & guardrails

- **First milestone = one QA eval, real model, through the full distributed path**, results queryable
  in ClickHouse. Agentic/sandbox is explicitly out of this milestone.
- **Cost-minimal:** zonal GKE (free control plane), one always-on `e2-medium`, spot workers that scale
  to **0** when idle, **no Cloud NAT**, **no LoadBalancer** (port-forward). Idle target ≈ $15–26/mo.
- **Model provider for e2e = OpenRouter** (already validated in the prototype) routed **through the
  LiteLLM gateway** — that exercises the current design's "all traffic gateway-fronted + canonical
  cost" path without needing a self-hosted model.
- **Portability preserved:** GCS via the S3 API, vanilla Postgres, everything Helm/Terraform.

---

## 1. Target topology (what runs where)

```
GKE zonal cluster (us-central1-a)
├── system pool  (1× e2-medium, always on)
│     ├── eval-engine-api        (FastAPI, Deployment)        — control plane
│     ├── eval-engine-orch       (Orchestrator, 1 replica)    — admit→expand→reconcile→finalize
│     ├── litellm                (gateway Deployment)         — all model traffic, Redis rate-limit
│     ├── clickhouse             (single pod + PVC)            — analytics (~12B-row table; tiny in test)
│     ├── redis                  (single pod)                  — gateway rate-limit + run stop-flags
│     └── KEDA operator                                       — autoscaler
├── workers pool (spot, 0..3 e2-medium, tainted)
│     └── eval-engine-worker     (Deployment, KEDA-scaled on count(queued))
└── (no in-cluster Postgres)
Managed Postgres: Neon serverless (free tier)  — metadata + ephemeral ledger
Object store:     GCS bucket  <project>-eval-engine  — .eval logs + transcripts (S3 API)
Registry:         Artifact Registry  us-central1/eval-engine  — the one app image
```

One image, role selected by command (`uvicorn … api`, `… orchestrator`, `… worker`). The control
plane and orchestrator co-locate on the always-on node; only workers use spot.

---

## 2. What already exists (reuse, don't rebuild)

- ☑ **Terraform** (`deploy/terraform/`) — zonal cluster, `system` + spot `workers` pools, GCS bucket,
  Artifact Registry repo. *Validated, plan = 8 resources.* **Not yet applied.** (Comments still say
  "Ray head/KubeRay" — fix to KEDA when we touch it.)
- ☑ **Code promoted to repo root** (out of `prototype/`) — package `eval_engine/`, `tests/`,
  `examples/`, `static/`, `infra/` at root; `deploy/sandbox/`; `pyproject.toml` (name `eval-engine`).
  Ray removed (rejected approach). Verified working post-move (CLI, mock run, concurrency test).
- ☑ **Dockerfile** (`deploy/Dockerfile`) — one image (root layout), deps layer cached, container-safe
  lazy `db.init`; roles by command (`uvicorn … api` / `python -m eval_engine.{orchestrator,worker}`).
- ☑ **Backends in the package** — `control_pg.py` (real Postgres `FOR UPDATE SKIP LOCKED`),
  `analytics_ch.py` (ClickHouse `ReplacingMergeTree`), selected by `EVAL_ENGINE_BACKEND=postgres`.
  `api.py` (FastAPI), `runner.py` (claim→execute→commit→batch-load→finalize), static dashboard.
- ☑ **GCP prereqs** — project `eval-engine`, billing linked, APIs enabled (container, artifactregistry,
  compute, storage), local `gcloud`/`kubectl`/`helm`/`terraform` authed.

**The gap to close (app-side):** the prototype is single-process (`api.py` does `launch()`+`execute()`
inline). The cluster needs three entrypoints over the shared ledger: **api** (launch only), **worker**
(claim→execute loop, no finalize), **orchestrator** (expand on launch + reconcile + finalize when all
terminal). The Ray split (`ray_executor.py`) already factored `_finalize()` out — reuse that shape,
drop Ray. The current claim path uses a **fixed per-run `max_inflight`** cap (no `run_slots`).

---

## 3. Milestones

### M0 — Infra up (`terraform apply`)  ◐
- [x] Plan validated (8 resources). GCS bucket `eval-engine-eval-engine`, Artifact Registry
      `us-central1-docker.pkg.dev/eval-engine/eval-engine`, and APIs created.
- [◐] `terraform apply` running — cluster + system pool + spot workers pool provisioning.
- [ ] `gcloud container clusters get-credentials eval-engine --zone us-central1-a` → kubeconfig.
- [ ] `kubectl get nodes` shows the system node (workers pool at 0 — expected).
- [x] Fixed terraform comments: Ray/KubeRay → KEDA/Deployment.
- **Cost starts here.** Verify the spot pool is at 0 nodes when idle.

### M1 — Stateful backends  ☑
- [ ] **Postgres (Neon):** create the free project; put the connection string in a k8s Secret
      (`eval-pg`). (Do **not** paste it into this doc or chat.) — **needs your Neon signup.**
- [x] **ClickHouse:** manifest `deploy/k8s/10-clickhouse.yaml` (single pod + 10Gi PVC, `clickhouse:8123`).
      → apply once the cluster is up.
- [x] **Redis:** manifest `deploy/k8s/11-redis.yaml` (single pod, `redis:6379`).
- [x] **Namespace:** `deploy/k8s/00-namespace.yaml` (`eval-engine`).
- [x] **Schema init:** `deploy/k8s/20-schema-init-job.yaml` ran `db.init()` → PG tables (runs,
      sample_tasks, failed_task_archive) on Neon + `sample_results` on ClickHouse. **Done.**
- [x] **ClickHouse network access fix:** the `:24.8` image restricts `default` to localhost; a
      `clickhouse-users` ConfigMap (`deploy/k8s/10-clickhouse.yaml`) opens it to the pod network.
- ⚠ **Rotate the Neon credential** — it was inadvertently printed (base64) to the session; rotate the
      `neondb_owner` password in the Neon console and re-create the `eval-pg` secret.
- [ ] Namespace `eval-engine`; Secrets: `eval-pg`, `openrouter` (the OpenRouter API key).
- [ ] **Schema init:** run the Postgres DDL (`SCHEMA.md` §1) + ClickHouse table (`SCHEMA.md` §2) — a
      one-shot `kubectl run` Job using the app image's `db.init` / a migration command.

### M2 — Build & push the image  ☑
- [x] `gcloud auth configure-docker us-central1-docker.pkg.dev`.
- [◐] Build + push `us-central1-docker.pkg.dev/eval-engine/eval-engine/app:<sha>` + `:latest`
      (`deploy/Dockerfile`, context = repo root).
- [ ] Confirm the image runs `uvicorn eval_engine.api:app` and the `python -m eval_engine.{worker,
      orchestrator}` entrypoints.

### M3 — App-side distributed split (code)  ☑
- [x] **RunSpec persistence** — `runs.spec_json` + `control.get_spec/active_runs/run_total` (both backends).
- [x] **Worker** (`eval_engine.worker`) — claim→execute→commit→load loop over active runs; pod name = claimer id.
- [x] **Orchestrator** (`eval_engine.orchestrator`) — admit queued→running; finalize when terminal
      (gate on authoritative `total`; safety-sweep load → aggregate → archive → prune → completed). One replica.
- [x] **API** — `POST /runs` launches only in the cluster (inline execute kept for local sqlite dev via env toggle).
- [x] **Local smoke** — `tests/test_distributed.py`: launch→admit→drain→finalize, exactly-once, ledger
      pruned — passes on **both sqlite and real Postgres+ClickHouse** (via `infra/up.sh`).
- Deferred to a later pass (not needed for the QA e2e): the `run:<id>:stop` Redis flag (cancel/budget),
  two-lane admission + per-run cap, live-metrics on the `runs` row.

### M4 — Model gateway (LiteLLM)  ☐
- [ ] LiteLLM Deployment + Service `litellm:4000`, configured to proxy **OpenRouter** (key from the
      `openrouter` Secret), with **Redis-backed** global rate limit + per-`run_id` cost tracking.
- [ ] Point workers' model calls at the gateway (`OPENAI_BASE_URL`/LiteLLM route) instead of calling
      OpenRouter directly — so the gateway is the canonical cost source (`DESIGN.md` §8).
- [ ] Smoke: one model call through the gateway returns + is counted.

### M5 — Workers + KEDA  ☐
- [ ] Worker Deployment on the spot pool (toleration for `eval-engine/worker`, nodeSelector
      `eval-engine/role=worker`), replicas 0.
- [ ] Install KEDA (Helm). `ScaledObject` with the **PostgreSQL scaler** on
      `count(*) FROM sample_tasks WHERE status='queued'` + a `maxReplicas` cap.
- [ ] Verify: launching a run scales workers 0→N, draining scales back to 0.

### M6 — QA e2e  ☐
- [ ] Seed one QA eval + dataset (reuse `examples/capitals_openrouter.yaml` shape) via the API.
- [ ] Launch a run; watch the `runs` row progress + live score; workers spin up on spot; results
      land in ClickHouse; ledger prunes to 0 on finalize.
- [ ] Read accuracy/cost back (canned CH query). **This is the milestone.**

### M7 — Access & teardown  ☐
- [ ] `kubectl port-forward` the API/dashboard for a look (no LoadBalancer).
- [ ] Document `terraform destroy` / scale-to-zero to stop spend between sessions.

---

## 4. Decisions & open questions

- **Postgres = Neon serverless (free tier)**, not in-cluster — chosen earlier for zero always-on DB
  cost. (Alternative: in-cluster CloudNativePG single pod — avoids an external dependency but adds
  memory pressure on the one `e2-medium`. Revisit if Neon's free limits bite.)
- **ClickHouse & Redis = single pods on the system node** (test-grade, not HA). Fine for e2e; HA is a
  later concern.
- **Leader election for the Orchestrator** — deferred for the e2e (one replica). Postgres advisory
  lock (`DESIGN.md`) when we want a standby.
- **Dashboard** — the prototype static page (port-forwarded) for now; Next.js is later.
- **System node = `e2-standard-4`** (4 vCPU/16GB, ~$98/mo) — **resolved at M1.** The `e2-medium`
  (2 vCPU) was CPU-bound: GKE system daemons reserve ~640m of ~940m allocatable, leaving ~137m, far
  short of the always-on stack (~500m). Adding RAM doesn't help (CPU is the limit); e2-standard-4
  gives ~3200m free, so the whole stack fits with no trimming or pod juggling.
- **M3 needs the full RunSpec persisted.** The prototype's `runs` table stores only
  model/harness-type/scorer-types — a separate worker process can't rehydrate `harness_config`,
  `scorer_config`, `dataset_slice`, `batch_size`. Add a `spec_json` column (or the `run_specs` table
  per SCHEMA §1.5) + `control.get_spec(run_id)` + `control.active_runs()`. This is the real substance
  of M3, alongside the entrypoint split.

---

## 5. Cost guardrails (check each session)

- Spot workers at **0** when no run is active (`kubectl get nodes` → only the system node).
- No LoadBalancer services (`kubectl get svc -A | grep LoadBalancer` → empty).
- No Cloud NAT.
- Between sessions: scale ClickHouse/Redis/LiteLLM to 0 or `terraform destroy` to stop the ~$/day on
  the always-on node.

---

## 6. Runbook (commands accrue here as we execute)

```bash
# M0
cd deploy/terraform && terraform apply
gcloud container clusters get-credentials eval-engine --zone us-central1-a
kubectl get nodes

# (further commands added as each milestone lands)
```
