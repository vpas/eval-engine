# Eval Engine — System Architecture

A Mermaid `architecture-beta` view of the deployed v1 system: a lightweight **control plane**
(humans + state) and a heavy, KEDA-scaled **execution plane** (compute), talking only through
Postgres + the ephemeral ledger. All model traffic is fronted by the LiteLLM gateway; every
completed sample is flattened into ClickHouse and its transcript/`.eval` log lands in object storage.

```mermaid
architecture-beta
    group access(internet)[Access edge]
    group control(cloud)[Control plane]
    group exec(cloud)[Execution plane]
    group gateway(cloud)[Model gateway]
    group store(cloud)[Storage tier]
    group external(internet)[Models]
    group obs(cloud)[Observability]

    service users(internet)[Users] in access
    service ingress(server)[nginx Ingress TLS] in access
    service oauth(server)[oauth2 proxy OIDC] in access

    service frontend(server)[Next js dashboard] in control
    service api(server)[FastAPI control plane] in control
    service orch(server)[Orchestrator leader] in control
    service monitor(server)[Training monitor] in control
    service viewer(server)[Inspect viewer] in control
    service pg(database)[Postgres meta plus ledger] in control

    service workers(server)[KEDA workers Inspect] in exec
    service sandbox(server)[Agentic sandbox gVisor] in exec

    service litellm(server)[LiteLLM gateway] in gateway
    service redis(database)[Redis HA rate limits] in gateway

    service clickhouse(database)[ClickHouse HA analytics] in store
    service objstore(disk)[Object store eval logs zstd] in store

    service providers(cloud)[API providers OpenRouter] in external
    service vllm(server)[Self hosted vLLM] in external

    service grafana(server)[Grafana] in obs
    service gmp(database)[Managed Prometheus] in obs

    users:R --> L:ingress
    ingress:R --> L:oauth
    oauth:R --> L:frontend
    oauth:B --> T:api
    frontend:B --> T:api

    api:B --> T:pg
    orch:R --> L:pg
    monitor:B --> T:orch
    api:R --> L:viewer

    orch:B --> T:workers
    workers:L --> R:pg
    workers:B --> T:litellm
    workers:R --> L:sandbox

    litellm:R --> L:redis
    litellm:B --> T:providers
    litellm:B --> B:vllm

    workers:B --> T:clickhouse
    workers:R --> L:objstore
    viewer:B --> T:objstore
    api:B --> B:clickhouse

    grafana:R --> L:gmp
    grafana:T --> B:clickhouse
    grafana:T --> B:pg
```

## Reading the diagram

**Access edge** — One public entrypoint: nginx Ingress (Let's Encrypt TLS via cert-manager) → all
traffic through **oauth2-proxy** (Google OIDC) → dashboard + API are both gated.

**Control plane** — `frontend` (Next.js: manage / launch / monitor / compare), `api` (FastAPI CRUD,
launch, status, analytics proxy, audit), `orch` (leader-elected admit + expand + reconcile +
finalize), `monitor` (training-checkpoint eval loop — launches ordinary tagged runs), `viewer`
(embedded Inspect log viewer for transcripts), and `pg` (Postgres: run/spec/entity metadata +
the **ephemeral `FOR UPDATE SKIP LOCKED` task ledger** — the sole scheduler).

**Execution plane** — Stateless `workers` (K8s Deployment, **KEDA**-scaled on ledger queue depth)
run a claimed shard as one Inspect Task: claim → execute → commit → load. Agentic harnesses spawn an
ephemeral per-sample **gVisor sandbox** pod. Workers don't coordinate — the ledger does.

**Model gateway** — **LiteLLM** (≥2 replicas) fronts *all* traffic (API providers via OpenRouter
today + self-hosted vLLM), with **Redis HA** holding shared global rate-limit state and the
per-`run_id` cost tally.

**Storage tier** — **ClickHouse** (HA, `ReplicatedReplacingMergeTree`, ~12B-row per-sample
projection; workers async-insert with durable ack *before* flipping the ledger to `done`) and the
**object store** (S3/GCS/MinIO via an fsspec abstraction) holding `.eval` logs + zstd transcripts.

**Observability** — Grafana (dashboards-as-code over ClickHouse + Postgres + Managed Prometheus);
GMP scrapes the fleet.
```
```

## Notes on `architecture-beta`

- Edge labels and free icon packs aren't used so this renders on a stock Mermaid install (only the
  built-in `cloud`/`database`/`disk`/`internet`/`server` icons). The grouping and port directions
  (`:L/:R/:T/:B`) carry the topology; see `DESIGN.md` §6 for the canonical ASCII diagram and rationale.
- The `architecture-beta` label parser rejects `.` and `-` inside `[...]` titles (a syntax error), so
  labels are spelled out (`Next js`, `Self hosted`) rather than `Next.js` / `Self-hosted`.
