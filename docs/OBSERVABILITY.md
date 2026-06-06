# Observability — Grafana dashboards (as code)

> The **time-series / historical** view of the platform. Complements the in-app **Ops tab**
> (`docs/OPS_DASHBOARD.md`), which is a *live health snapshot* ("is everything up right now?").
> Grafana answers "how did pass-rate / cost / throughput / fleet size move over time?" Everything —
> datasources and dashboards — is provisioned **as code**; the only UI interaction is viewing.

## Why this split (Grafana vs the Ops tab)

| | Ops tab (`/ops`) | Grafana (`/grafana`) |
|---|---|---|
| Question | Is each subsystem healthy *now*? | How did metrics *trend*? |
| Shape | one snapshot, polled every 4s | time-series, range-queried, alertable |
| Source | `ops.snapshot()` (live probes) | ClickHouse + Postgres + Prometheus |
| Lives in | Next.js app | Grafana, behind the same OAuth gate |

## Datasources (3, all provisioned)

Provisioned by `deploy/k8s/95-grafana.yaml`. An init container parses the Neon DSN (from the
`eval-pg` secret) into the Postgres datasource; ClickHouse + Prometheus are static.

| Datasource | Backs | Role |
|---|---|---|
| **ClickHouse** (`grafana-clickhouse-datasource`) | `sample_results`, `run_summary` | The warehouse — eval quality, cost, tokens, latency, all sliceable by model/eval/category. |
| **Postgres** (built-in) | `runs`, `sample_tasks`, `heartbeats`, `audit_log` | The control plane — live fleet/queue/ledger state, budget burn, orchestration internals. |
| **Prometheus (GMP)** (built-in) | LiteLLM `/metrics` + GKE system metrics | Gateway QPS/latency/spend + pod CPU/mem/replicas/restarts/nodes. Via the GMP query frontend. |

## The five dashboards

Sources in `deploy/grafana/dashboards/*.jsonnet`. Render with `make render` (see that folder's README).

1. **Quality** (`eval-quality`) — pass rate over time by model, mean primary score, error rate by
   `error_type`, throughput (samples/min), per-category pass rate, attempt/retry distribution, and a
   model×eval leaderboard. Template vars: `eval`, `model` (multi-select).
2. **Cost & Tokens** (`cost-tokens`) — total spend + cost/1k-tokens, cost rate by provider, cumulative
   spend, token throughput, cost-by-model table, and **live budget burn** per in-flight run
   (`runs.total_cost_usd` ÷ `run_specs.budget.max_usd`).
3. **Fleet & Orchestration** (`fleet`) — running/queued runs, in-flight ledger tasks, active workers,
   runs/ledger by status, **orchestrator tick latency** + **worker claims/loop** (from `heartbeats`),
   expired leases (dead-worker reclaim) + max attempts (poison samples), runs launched/hr, audit
   activity, and a recent-run timings table (queue wait + duration).
4. **Model Gateway** (`gateway`) — request rate + failed-request rate by model, p50/p95/p99 latency,
   token rate, and gateway-tallied spend. From LiteLLM's exporter.
5. **Infra** (`infra`) — **worker replicas over time (KEDA 0→N)**, node count, ready-vs-desired per
   deployment, pod restart rate, memory working-set + CPU per pod (watch LiteLLM/ClickHouse), PVC usage.

## Setup steps (one-time, beyond `kubectl apply`)

The manifests are declarative, but three things need a human/IAM touch:

1. **Render the dashboards** before first apply (no toolchain in the image):
   `cd deploy/grafana && make render && kubectl apply -f build/grafana-dashboards.configmap.yaml`.
2. **GMP frontend IAM** — `gmp-frontend` queries Managed Prometheus using the node SA's ADC. Grant
   the node service account `roles/monitoring.viewer` (project-level) or it returns 403s.
3. **GKE managed collection** for the infra dashboard — the LiteLLM scrape works out of the box
   (`PodMonitoring`, `96-podmonitoring.yaml`), but the *system* series the Infra dashboard uses
   (`kube_*`, `container_*`, `kubelet_volume_*`) require GMP's managed **kube-state-metrics** and
   **kubelet/cadvisor** packages enabled. Apply the `ClusterPodMonitoring`/`OperatorConfig` for those
   per the [GMP managed-collection docs](https://cloud.google.com/stackdriver/docs/managed-prometheus/setup-managed).
   Until then the Quality/Cost/Fleet/Gateway dashboards are fully functional (they don't depend on it).

Optional secret for break-glass admin login (auth-proxy is the normal path):
`kubectl create secret generic grafana -n eval-engine --from-literal=admin-password=<pw>`.

## App `/metrics` exporter — deliberately deferred

The industry standard for app metrics is an in-process Prometheus client exposing `/metrics`
(counters/histograms with correct rate/percentile semantics), scraped by Prometheus — versus
reconstructing metrics by querying the operational DB. We use the **DB/SQL path for domain metrics**
and **Prometheus for gateway + infra**, and *skip* a dedicated app exporter for now, because:

- The orchestration internals an exporter would add (`tick_ms`, `leader`, `running_runs`,
  `claimed_this_loop`) are **already persisted** in the `heartbeats` table → Grafana trends them via
  Postgres directly (Fleet dashboard).
- The metrics that genuinely need real counters/histograms (gateway QPS/p99/spend, pod CPU/mem) come
  from **LiteLLM + GMP** with no app code.

The hook if we later want alerting-grade counters: add `eval_engine/metrics.py` exposing a
`prometheus_client` registry on the API (re-publishing `ops.py` probes + orchestrator counters) and a
`PodMonitoring` for it. Trade-offs: + correct counter semantics, decoupled from DB load, uniform with
infra; − code to maintain, cardinality care (never label by `run_id`), another scrape target.

## Cost

One ~256Mi Grafana pod + a tiny GMP frontend on the always-on `e2-standard-4` (which has ~3200m
headroom). **No Prometheus server to run** — we reuse Google Managed Prometheus — so the footprint is
just Grafana itself. Both scale to 0 with the system pool during `infra/cloud-down.sh`.
