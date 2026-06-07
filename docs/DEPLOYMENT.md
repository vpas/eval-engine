# Eval Engine — GCP Deployment & Implementation Tracker

> **Living progress doc.** Goal: stand up the **current v1 design** (`DESIGN.md`) on GKE and run a
> real eval **end-to-end**, spending as little as possible. First target is a **QA-only e2e** (no
> agentic sandbox — that's `docs/FUTURE.md` §4). Update the status boxes as we go.
>
> Status legend: ☐ todo · ◐ in progress · ☑ done · ⊘ deferred.

Last updated: 2026-06-05.

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
- [x] **API** — `POST /runs` launches only in the cluster (inline execute kept for local single-process dev via env toggle).
- [x] **Local smoke** — `tests/test_distributed.py`: launch→admit→drain→finalize, exactly-once, ledger
      pruned — passes against **Postgres + ClickHouse** (via `infra/up.sh`).
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
- [x] **Wired into the OIDC ingress** at **`/inspect/`** (header **"traces ↗"** link) — live at
      **https://35-202-212-111.nip.io/inspect/** behind Google auth, no port-forward. Integration
      surfaced + fixed 4 issues: (1) the viewer's absolute `/api/*` collided with our app's → app moved
      to **`/be/*`**, viewer keeps `/api/*`; (2) `/inspect/` redirect loop → dropped the self-matching
      redirect + `skipTrailingSlashRedirect`; (3) **inspect-ai + gcsfs `datetime`-mtime crash** when
      listing `gs://` logs → patched viewer entrypoint (`eval_engine/view_main.py`); (4) deep-link uses
      the **full `gs://` log path**, not basename.
- [x] **Per-sample deep-link:** workers record each sample's `.eval` log path (`eval_log_uri`) in its
      transcript; the dashboard's sample drawer has a **"full trace ↗"** link → `/inspect/?log_file=<that
      log>`, opening that exact sample's rich Inspect trace.
- [x] **Deep-link "Failed to fetch" — root cause + fix (inspect-ai version bump):** the deployed viewer
      was **inspect-ai 0.3.69** (frozen by the cached Docker dependency layer, not a pin — `pyproject` only
      said `inspect-ai`). That old client sends the **full `gs://…` log path** to `/api/log-size/<path>`;
      **oauth2-proxy** (Go `net/http`, runs `path.Clean`) decodes `%2F` and collapses the `gs://` `//` → `gs:/`
      via a **301 redirect** → inspect-view 404 → "Error: Failed to fetch". (nginx `merge_slashes` was a
      red herring; the collapse is in oauth2-proxy's redirect, not nginx request routing.) **Fix:** pin
      **`inspect-ai>=0.3.235`** in `pyproject.toml` (busts the cached layer + documents the requirement).
      The 0.3.235 client builds every API URL via `directoryRelativeUrl(file, log_dir)`, which strips the
      `gs://<bucket>/eval-logs/` prefix to a **single-segment basename** (`2026-…eval`, no `/`) — so
      `/api/log-bytes/2026-…eval` has nothing for `path.Clean` to collapse, and the deep-link's full-`gs://`
      `?log_file=` is stripped the same way. Bonus: 0.3.235 also fixes the gcsfs `datetime`-mtime crash.
- [x] **The actual sufficient fix — relative log names via a viewer `mapping_policy` (`view_main.py`):**
      the version bump alone was *necessary but not sufficient*. inspect reads `.eval` files (`isEvalFile`)
      through `openRemoteLogFile`, and the api it uses is chosen once by `resolveApi`: a bare `?log_file=`
      → `staticHttpApi` (browser does `fetch("gs://…")` → unsupported scheme → "Failed to fetch", never
      hits the server); `?inspect_server=true` → `viewServerApi` (server reads gs://, proxies bytes). **But**
      `viewServerApi` builds `/log-info/${encodeURIComponent(file)}`, so a full `gs://` *still* yields
      `%2F%2F` that **oauth2-proxy** (Go `path.Clean`) collapses via a 301 — so *every* `.eval` read (even a
      sidebar click) was broken through the ingress, not just the deep-link. **Fix:** `view_main.py` injects
      a `FileMappingPolicy` + matching access policy (monkeypatches `view_server_app`, which `view_server`
      resolves by module-global name) so the client only ever sees/sends a **relative basename**
      (`2026-…eval` — no scheme, no `/`); the server maps it back to `gs://<log_dir>/<name>` to read+proxy
      server-side. The dashboard deep-link now passes `?log_file=<basename>&inspect_server=true`
      (`frontend/app/runs/[id]/page.tsx`). Verified through the frontend proxy: `/api/log-files` returns
      relative names; `/api/log-info/<basename>` + `/api/log-bytes/<basename>` → 200 (server resolves gs://).
- [x] **Final piece — empty `/log-dir` (`view_main.py`):** the client re-expands a relative name with
      `join(name, logDir)` where `logDir` comes from `GET /log-dir`; `join` returns the name unchanged
      only when `logDir` is empty. That route never unmaps, so it returned the raw `gs://` dir → the client
      rebuilt the full `gs://` path (and the `//` collapse came back as an HTTP 404). We monkeypatch
      `get_log_dir` to report `log_dir=""` for `gs://` dirs (listing still works — the client omits the
      `log_dir` query param when it's empty, so the server falls back to its internal `default_dir`). Now
      `join(basename, "")` = `basename` stays relative end-to-end. Verified: `/api/log-dir` → `{"log_dir":""}`,
      `/api/log-files` → relative names, `/api/log-info/<basename>` → 200. This also fixes plain sidebar
      clicks (same `join` path), so the viewer is fully usable through the ingress.
      Rebuilt + pushed `app:latest` (mapping + empty-/log-dir) and `frontend:latest` (deep-link); rolled both out.
      The `merge_slashes off;` `http-snippet` on the `ingress-nginx-controller` ConfigMap is harmless
      leftover defense-in-depth (not load-bearing — the relative names mean there are no `//` to collapse).

### Agentic / K8s sandbox in-cluster — WORKING (in-cluster proof, runc isolation)  ☑
The full agentic path now runs **end-to-end on the cluster**: a real model drives a `bash` tool whose
calls execute **inside an ephemeral, per-sample Kubernetes pod** that `inspect-k8s-sandbox` helm-installs
into the `eval-sandbox` namespace, then tears down on exit.

Built the whole path: image has **helm 3.16 + inspect-k8s-sandbox**; harness `sandbox: k8s`;
`deploy/k8s/90-sandbox-rbac.yaml` (eval-sandbox ns + worker SA + Role incl. `cilium.io`); a values
override (`deploy/sandbox/k8s-agent-env-values.yaml`). Fixed **3 blockers** the `agent-env` chart assumes
(it targets GKE **Dataplane-V2/Cilium** + GKE **Sandbox/gVisor**): the `CiliumNetworkPolicy` CRD, the
`cilium.io` RBAC, and `runtimeClassName: CLUSTER_DEFAULT` (no gVisor pool) — after which **`helm install`
+ a real sandbox pod were created**.

The **"4th blocker"** from the previous pass (worker Python k8s client → `ConfigException: No
configuration found`) is **gone**: it was an artifact of the stale frozen image, resolved by the
`inspect-ai>=0.3.235` rebuild (which also pulled a newer `k8s_sandbox` + kubernetes client that loads
in-cluster config correctly). Verified the worker pod mounts its SA token and sees
`KUBERNETES_SERVICE_HOST`; no code change was needed.

**Proof (2026-06-05):**
- **Synchronous** (`cli run`, single sample) — passed 100%; events show the `agent-env-…` StatefulSet
  scheduled in `eval-sandbox`, image pulled, container ran, torn down.
- **Distributed** (the real path: `POST /runs` → orchestrator admit → worker claim → execute → finalize),
  `examples/agentic_sandbox_k8s.yaml`, dataset `examples/sandbox_qa.jsonl` — **2/2 passed, accuracy 1.0**.
  `sb1`'s answer is `in-sandbox-7f3a9c`, a secret present **only** in the sandbox pod (injected via the
  chart values `services.default.env`, absent from the worker env and the prompt) — so a passing `sb1`
  is itself proof the tool ran **inside** the pod. `sb2` (compute 21+21) proves in-pod execution too.
- **Agentic uses `batch_size: 1`** (one sample per Inspect eval → one sandbox pod, no concurrency).
  This is the right default for high-variance agentic (DESIGN.md §"batch size"); with `batch_size>1`
  two samples run concurrently inside one eval and **deadlock contending on the k8s sandbox** (observed:
  worker blocked in asyncio, ~3 model calls then silence, sandbox pod idle). QA harnesses (uniform,
  sandbox-free) are the ones that batch large.

**Isolation caveat:** `CLUSTER_DEFAULT` = **runc**, weaker than the production **T2 gVisor** tier. This
cluster has no GKE-Sandbox (gVisor) pool and no Dataplane-V2 (both ~free but **set at cluster creation**,
so a full prod run = a cluster rebuild — see `docs/FUTURE.md` §4). Switchability is cheap where it counts:
a **gVisor node pool can be added to this cluster later** (`--sandbox type=gvisor`) and the runtime flipped
back in `k8s-agent-env-values.yaml` with **no recreation**; only the Dataplane-V2 / native
`CiliumNetworkPolicy` air-gap is creation-locked (standard `NetworkPolicy` via the Calico addon is an
in-place alternative). So the cheap proof is a real superset-later, not a dead end.

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

### Pause / resume (overnight cost control)
- **`infra/cloud-down.sh`** — scales the fixed `system` node pool to 0 (stops compute billing). Evicts
  all pods but keeps the cluster, PVCs, config, static IP and images; `workers` already autoscales to 0.
- **`infra/cloud-up.sh`** — scales `system` back to 1 and waits for the core deployments to reschedule
  (no redeploy — Deployments persist across the pause).
- Still billing while paused (negligible, needed for clean restore): control-plane mgmt fee (~$0.10/hr),
  the ingress LB + static IP (deleting it would change the nip.io host → break OAuth), persistent disks.
  Full stop = `terraform destroy` (teardown, not an overnight pause).
- Note: `up.sh`/`down.sh` are the **local-dev** Postgres+ClickHouse backends (test suite), not the cloud.

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

---

## 7. Fresh-cluster bring-up / migration (declarative)

A new cluster is now **three steps**, with cluster-specific values resolved from Terraform — not
hand-edited into manifests.

```bash
# 1. Infra + add-ons + reserved IP/IAM (Managed Prometheus, KEDA, ingress-nginx pinned to the IP,
#    cert-manager). The nip.io host is derived from the reserved IP and is STABLE across rebuilds.
cd deploy/terraform && terraform apply
terraform output -raw host          # e.g. 35-202-212-111.nip.io — register ONCE as the OAuth redirect host

# 2. Secrets (from env; never committed). See deploy/secrets.sh header for the required vars.
EVAL_ENGINE_PG_DSN=… OPENROUTER_API_KEY=… OAUTH2_CLIENT_ID=… OAUTH2_CLIENT_SECRET=… ../secrets.sh

# 3. Render + apply manifests (envsubst ${EE_HOST}/${EE_PROJECT}/…) + dashboards + restarts.
export PATH=$PATH:/usr/local/go/bin:$HOME/go/bin   # for the grafonnet render
../install.sh
```

**What Terraform owns now:** cluster + node pools + GCS + Artifact Registry (as before) **plus** the
reserved external IP (`google_compute_address`), IAM bindings (node SA → `monitoring.viewer` +
bucket `objectAdmin`), Managed Prometheus, and the KEDA / ingress-nginx / cert-manager Helm releases
(`network.tf` / `iam.tf` / `helm.tf`). Outputs `host`, `lb_ip`, `project_id` feed `install.sh`.

**What stays scripted:** the app manifests (applied in `deploy/manifests.txt` order; the 4 host/project
ones rendered via `envsubst`) and secret creation — both in `deploy/install.sh` + `deploy/secrets.sh`.

**Why migration is cheap:** the LB IP is reserved, so the host (and the Google OAuth redirect URI) is
stable — you register it once and never touch OAuth on a rebuild.

**Rerunning on the EXISTING cluster (no `terraform apply`):** `install.sh` is idempotent — every step
is `kubectl apply` (a no-op when unchanged), and litellm carries a `config-hash` annotation so it rolls
*only* when its manifest changes (no blind restart). There's no `terraform output` to read, so export
the current values first:
```bash
export EE_HOST=35-202-212-111.nip.io EE_PROJECT=eval-engine EE_CLUSTER=eval-engine EE_ZONE=us-central1-a
deploy/install.sh        # safe to rerun; IAM (monitoring.viewer) is a one-time idempotent gcloud grant
```

**Remaining manual / same-project assumptions:**
- The **OAuth client** (consent screen, test users, the web client id/secret) is created once in the
  GCP console — not Terraform-able. With a reserved IP it's a one-time setup, not per-migration.
- The image registry path + GCS bucket name still embed the project literally in the manifests
  (`…/eval-engine/…`, `eval-engine-eval-engine`). Cluster rebuilds within the same project are
  unaffected; a cross-**project** move would also need those templated (deferred — not the common case).
- Adopting the Helm releases on the **existing** cluster (already `helm install`ed manually) needs
  `terraform import` (or accept Helm reconciling them); fresh clusters install clean.
