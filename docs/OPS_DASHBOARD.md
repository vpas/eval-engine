# Operational Dashboard — design & implementation

> A live "is the whole system healthy?" view for operators: every subsystem (control API,
> orchestrator, workers, Postgres, ClickHouse, Redis, LiteLLM, GCS, Inspect viewer, the Kubernetes
> workloads + KEDA), the live queue/admission state, active runs, recent failures, and **one-click
> deep links into GCP Log Explorer** for any component or run. New tab in the Next.js dashboard.

## Why a heartbeat backbone (not just the K8s API)

The control plane already talks to Postgres + ClickHouse + GCS, but **not** to Kubernetes, Redis, or
LiteLLM. The orchestrator (a leader-elected singleton) and the KEDA-scaled workers were previously
observable only through container logs. So the design uses **two signal tiers**:

- **Tier 1 — portable application health** (no cloud coupling): probes the API can already do
  (PG/CH/GCS) + a **`heartbeats` table** the orchestrator and each worker upsert every loop. This is
  the liveness backbone — it works identically on GKE/EKS/AKS/local, matching the repo's
  "portable by interface" principle, and the ops tab is fully functional with only this tier.
- **Tier 2 — cloud infra truth** (graceful enrichment): the in-cluster **Kubernetes API** for pod
  phase / restart counts / ready-replicas + KEDA desired-vs-current, and **GCP Log Explorer** deep
  links. Absent in dev (no in-cluster config / no project) → those bits silently no-op.

No log *contents* flow through the app — we hand the operator a pre-filtered Log Explorer URL (no
extra storage, quota, or PII surface) and let Cloud Logging tail it.

## Backend

| Piece | File | What |
|---|---|---|
| Heartbeat table + ops queries | `eval_engine/control.py` | `heartbeats(component,instance,ts,detail)`; `heartbeat()`, `list_heartbeats()`, `prune_heartbeats()`, `global_ledger_counts()`, `run_status_counts()`, `pg_connections()`, `active_runs_detail()`, `recent_failures()` |
| CH liveness | `eval_engine/analytics.py` | `health()` — row gauge + replica quorum/lag from `system.replicas` (HA) |
| Heartbeat writers | `orchestrator.py`, `worker.py` | orchestrator writes `{leader, running_runs, tick_ms}` each tick (standby writes `leader:false`); worker writes `{claimed_this_loop}` each loop. Worker also logs `run_id=…` so per-run log links match |
| Aggregator | `eval_engine/ops.py` | `snapshot()` runs all probes concurrently (per-probe timeout, isolated failures), `log_url(...)` builds Log Explorer links |
| Endpoints | `eval_engine/api.py` | `GET /ops/status` (full snapshot), `GET /ops/logs?component=&pod=&run_id=&severity=&minutes=` (one link) |
| RBAC | `deploy/k8s/41-api-rbac.yaml` | read-only `get/list` on pods, deployments, replicasets, `keda.sh/scaledobjects`; bound to the new `eval-engine-api` SA in the app + sandbox namespaces |
| Deps | `pyproject.toml` `[ops]` | `redis`, `kubernetes` (both optional — degrade to `unknown`/omitted) |

**Component status** is one of `ok | degraded | down | idle | unknown`. `overall` = `down` if a
**critical** backend (Postgres/ClickHouse) is down, else `degraded` if anything is down/degraded, else
`ok`. Each probe is wrapped so an unreachable service becomes a `down` card, never a 500.

### GCP Log Explorer deep links

`ops.log_url(...)` builds:

```
https://console.cloud.google.com/logs/query;query=<URL-encoded LQL>;duration=PT<minutes>M?project=<project>
```

The LQL filter is composed from `resource.type="k8s_container"` + cluster/namespace + an optional
`container_name` / `labels."k8s-pod/app"` / `pod_name`, an optional free-text `run_id` match, and an
optional `severity>=`. Returns `None` when `EVAL_ENGINE_GCP_PROJECT` is unset (dev) → the UI hides the
button. Configured via the API deployment env: `EVAL_ENGINE_GCP_PROJECT`, `EVAL_ENGINE_GKE_CLUSTER`,
`EVAL_ENGINE_GKE_ZONE`, `EVAL_ENGINE_K8S_NAMESPACE` (set in `40-control-plane.yaml`). **Set
`EVAL_ENGINE_GCP_PROJECT` to the real GCP project id** for the links to resolve.

## Frontend

- New **Operations** tab (`components/appbar.tsx`) → `app/ops/page.tsx`, polling `/be/ops/status` every 4s.
- Layout: health banner (overall + cluster meta + namespace Log Explorer link) → 4 stat tiles
  (health / throughput / in-flight / active runs) → **component cards** (status pill, metrics chips,
  per-component Logs ↗) → **Queue & admission** panel (ledger bars + two-lane utilization vs the
  interactive reserve) → **Active runs** table (per-run Logs ↗, drill to run page) → **Kubernetes
  workloads** (ready/desired + per-pod restart badges + per-pod Logs ↗) → **Recent failures** +
  **Recent activity** (audit) feeds.
- `lib/api.ts`: `OpsStatus`/`OpsComponent`/… types, `getOps()`, `getRunLogsUrl()`.
- Run-detail page (`app/runs/[id]/page.tsx`) gains a **Worker logs ↗** button (run-scoped link).

## Tests

`tests/integration/test_ops.py` — log-link building + gating, heartbeat-derived liveness
(ok/degraded/idle), the `/ops/status` snapshot shape + graceful degradation, and `/ops/logs`.
`heartbeats` added to the per-test truncation set in `tests/conftest.py`.

## Possible follow-ups (not built)

- Gate `/ops/*` to admin emails (today: same OIDC as the rest of the app).
- Per-pod CPU/mem from `metrics.k8s.io` (metrics-server).
- True samples/s from a CH-backed window instead of the single-replica in-memory delta cache.
