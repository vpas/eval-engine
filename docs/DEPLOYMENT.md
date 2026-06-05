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
- **Cost-minimal:** zonal GKE (free control plane), one always-on `e2-standard-4` (the stack is
  CPU-bound below that), spot workers that scale to **0** when idle, **no Cloud NAT**, **no
  LoadBalancer** (port-forward). Always-on ≈ $98/mo; teardown/scale-down between sessions.
- **Model provider for e2e = OpenRouter** (already validated in the prototype) routed **through the
  LiteLLM gateway** — that exercises the current design's "all traffic gateway-fronted + canonical
  cost" path without needing a self-hosted model.
- **Portability preserved:** GCS via the S3 API, vanilla Postgres, everything Helm/Terraform.

---

## 1. Target topology (what runs where)

```
GKE zonal cluster (us-central1-a)
├── system pool  (1× e2-standard-4, always on)
│     ├── eval-engine-api        (FastAPI, Deployment)        — control plane
│     ├── eval-engine-orch       (Orchestrator, 1 replica)    — admit→expand→reconcile→finalize
│     ├── litellm                (gateway Deployment)         — all model traffic, Redis rate-limit
│     ├── clickhouse             (single pod + PVC)            — analytics (~12B-row table; tiny in test)
│     ├── redis                  (single pod)                  — gateway rate-limit + run stop-flags
│     └── KEDA operator                                       — autoscaler
├── workers pool (spot, 0..3 e2-medium, tainted)  ← scale-to-zero
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
  Artifact Registry repo. **Applied (M0).**
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

### M0 — Infra up (`terraform apply`)  ☑
- [x] `terraform apply` — cluster + system pool (**e2-standard-4**) + spot workers pool (0..3) + GCS
      bucket `eval-engine-eval-engine` + Artifact Registry. Credentials fetched; `kubectl get nodes`
      shows the one system node (workers at 0 — correct).
- [x] Fixed terraform comments: Ray/KubeRay → KEDA/Deployment.
- **Cost is live** (~$98/mo always-on). Spot workers at 0 when idle.

### M1 — Stateful backends  ☑
- [x] **Postgres (Neon):** free project created; DSN in the `eval-pg` k8s secret (key `dsn`).
- [x] **ClickHouse:** `deploy/k8s/10-clickhouse.yaml` (single pod + 10Gi PVC, `clickhouse:8123`) Running.
- [x] **Redis:** `deploy/k8s/11-redis.yaml` (single pod, `redis:6379`) Running.
- [x] **Namespace:** `deploy/k8s/00-namespace.yaml` (`eval-engine`).
- [x] **Schema init:** `deploy/k8s/20-schema-init-job.yaml` ran `db.init()` → PG ledger tables on Neon
      + `sample_results` on ClickHouse. **Verified.**
- [x] **ClickHouse network-access fix:** the `:24.8` image pins `default` to localhost; a
      `clickhouse-users` ConfigMap opens it to the pod network.
- [ ] **`openrouter` secret** (the API key) — needed for M4/M6, not yet created.
- **Neon credential** — was printed (base64) to a session once; **rotation skipped by decision** (it's
      a throwaway testing DB and the exposure was only to Anthropic, not public). Accepted risk.

### M2 — Build & push the image  ☑
- [x] `gcloud auth configure-docker us-central1-docker.pkg.dev`.
- [x] Built + pushed `…/eval-engine/app:<sha>` + `:latest` (`deploy/Dockerfile`, context = repo root).
- [x] Entrypoints verified to boot (`uvicorn … api`, `python -m eval_engine.{worker,orchestrator}`).

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

### M4 — Model gateway (LiteLLM)  ☑
- [x] LiteLLM Deployment + Service `litellm:4000` (`deploy/k8s/30-litellm.yaml`) proxying **OpenRouter**
      (key from the `openrouter` secret), **Redis-backed**. (Bumped mem to 2.5Gi — OOMKilled at 1Gi.)
- [x] Workers call the gateway OpenAI-compatible (`OPENAI_BASE_URL=http://litellm:4000/v1`); a run with
      `model: openai/llama-3.1-8b` routes worker → LiteLLM → OpenRouter.
- [x] **A5 (pragmatic): `cost_usd` is now real for gateway calls.** LiteLLM uses wildcard passthrough
      (`*`→`openrouter/*`), and `runner._cost_usd` prices `openai/<id>` from the OpenRouter catalog —
      identical $ since the gateway fronts OpenRouter at catalog price. Verified: a gateway run shows
      `cost_usd=1.59e-06`. **Deferred (canonical A5):** the gateway's own per-`run_id` tally as the
      source (LiteLLM spend-DB + `run_id`-tagged requests + query at finalize) — needs tagging threaded
      through Inspect; worker-catalog price == gateway price here, so cost is correct meanwhile.

### M5 — Workers + KEDA  ☑
- [x] Worker Deployment on the spot pool (`deploy/k8s/50-worker.yaml`).
- [x] KEDA (Helm) + `ScaledObject` (`deploy/k8s/70-keda.yaml`): PostgreSQL scaler on
      `count(*) … WHERE status IN ('queued','running')` (keeps workers up while in-flight, not just
      queued), min 0 / max 3, `TriggerAuthentication` → `eval-pg` DSN.
- [x] **Verified end-to-end:** idle → worker scales to **0** (spot node drains); a run → KEDA scales
      **0→1**, processes, finalizes, scales back to **0**.

### M6 — QA e2e  ☑
- [x] **Mock run** through the full path: api→Neon ledger→spot worker→ClickHouse→finalize (1/3, ledger
      pruned).
- [x] **Real run through the gateway**: `openai/llama-3.1-8b` → LiteLLM → OpenRouter, **3/3 = 100%**,
      74 real tokens, ledger pruned. **Milestone met.**

### M7 — Access & teardown  ◐
- [x] `kubectl port-forward svc/eval-engine-api 8077:8077` to launch/read runs (no LoadBalancer).
- [ ] Document `terraform destroy` / scale-to-zero to stop spend between sessions (KEDA covers workers;
      ClickHouse/Redis/LiteLLM/api/orch on the always-on node still cost while up).

### M9 — Leader-election + transcripts to GCS  ☑
- [x] Orchestrator leader-election (Postgres advisory lock; safe for >1 replica).
- [x] Transcripts → GCS bucket when `EVAL_ENGINE_GCS_BUCKET` set; `GET /transcript` serves them;
      node SA granted `storage.objectAdmin`. Verified write+read in-cluster.

### M10 — Next.js dashboard  ☑
- [x] `frontend/` — Next.js App Router app (dark lab-instrument theme): runs list (stats, status
      pills, accuracy bars), run detail (metrics, category bars, samples + transcript drill-in),
      launch drawer from `/catalog`. Proxies `/api/*` → eval-engine-api; `/api/me` shows the OIDC user.
- [x] `deploy/k8s/80-frontend.yaml`; oauth2-proxy upstream rewired → the frontend (inherits Google
      auth + forwards `X-Auth-Request-Email`). Verified serving + proxy + still-gated through the ingress.

### M11 — Inspect log viewer (rich trace UI)  ☑
- [x] Workers write Inspect `.eval` logs to `gs://<bucket>/eval-logs` (gcsfs); `deploy/k8s/85-inspect-view.yaml`
      runs `inspect view start` over them (`enableServiceLinks:false` — the `INSPECT_VIEW_PORT` service-link
      env collided with `--port`). Verified serving (`Inspect View: gs://…/eval-logs · :7575`).
- [x] **Wired into the OIDC ingress** at **`/inspect/`**: the Next.js frontend proxies `/inspect/*` →
      inspect-view (its assets/API are relative, so the prefix strips cleanly; `skipTrailingSlashRedirect`
      keeps the trailing slash, no redirect). A **"traces ↗"** link in the dashboard header. No
      port-forward needed — live at **https://35-202-212-111.nip.io/inspect/** behind Google auth.

### Agentic / K8s sandbox in-cluster — SCAFFOLDED, blocked on cluster setup (documented)
Built the whole path: image has **helm 3.16 + inspect-k8s-sandbox**; harness `sandbox: k8s`;
`deploy/k8s/90-sandbox-rbac.yaml` (eval-sandbox ns + worker SA + Role incl. `cilium.io`); a values
override (`deploy/sandbox/k8s-agent-env-values.yaml`). Fixed **3 blockers** the `agent-env` chart assumes
(it targets GKE **Dataplane-V2/Cilium** + GKE **Sandbox/gVisor**): the `CiliumNetworkPolicy` CRD, the
`cilium.io` RBAC, and `runtimeClassName: CLUSTER_DEFAULT` (no gVisor pool) — after which **`helm install`
+ a real sandbox pod were created**. A 4th blocker remains in the worker (the Python k8s client's
in-cluster config: `ConfigException: No configuration found`). **The clean path is a cluster built for it**
— GKE Dataplane-V2 + a GKE-Sandbox (gVisor) node pool (terraform `datapath_provider=ADVANCED_DATAPATH`
+ a `sandbox_config` pool), i.e. a cluster recreation. That's the right next step; piecemeal-patching
the cost-minimal cluster is fighting the tool. The prototype already proved the agentic *contract* on
local Docker; this is purely the cloud-infra exercise.

### Canonical A5 (gateway cost tally) — DEFERRED (documented)
The pragmatic A5 (real `cost_usd` from the OpenRouter catalog the gateway fronts) is done and correct.
The *canonical* form (gateway's own per-`run_id` spend as the source) needs a LiteLLM spend-DB +
tagging every request with `run_id` threaded through Inspect's model call (not cleanly exposed) +
a finalize-time query. Larger change, uncertain payoff here (worker-catalog price == gateway price),
so it stays deferred.

### M8 — External access + OIDC (Google)  ☑
- [x] **ingress-nginx** (Helm) → external LB `35.202.212.111`; host `35-202-212-111.nip.io` (nip.io).
- [x] **cert-manager** (Helm) + `letsencrypt-prod` ClusterIssuer → TLS cert issued (HTTP-01).
- [x] **oauth2-proxy** (Google OIDC, `deploy/k8s/61-oauth2-proxy.yaml`) gates the API; allowlist =
      consent-screen Test Users **and** an emails file (victor.passichenko@…, ravenkklo@…).
- [x] **Ingress** (`62-ingress.yaml`) on the nip.io host, TLS, all traffic → oauth2-proxy → api.
- [x] Verified: unauth `/` → 403; `/oauth2/start` → 302 to Google with the right client/redirect.
- [ ] **Browser login confirmation** (user): sign in at `https://35-202-212-111.nip.io`.
- [x] **`created_by` wired** — API reads `X-Auth-Request-Email` → stored on each run + shown in the dashboard "by" column (closes part of D6).
- Cost: ingress LB ~$18/mo (the external-access tax). The api Service stays ClusterIP — reachable
  only through the authenticated proxy.

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
