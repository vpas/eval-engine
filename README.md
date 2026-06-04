# eval-engine

A modular, distributed engine for **model evaluations** at scale, plus a web dashboard to
author, launch, monitor, and analyze runs. Composability is the core principle: *model*,
*harness*, *dataset*, and *scorer* are independent blocks, and any valid combination works —
across QA, multi-turn agentic, LLM-as-judge, and custom-code scoring.

## Design docs

| Doc | Contents |
|---|---|
| [DESIGN.md](DESIGN.md) | Architecture, scale envelope, component decisions, decision log (D1–D10) |
| [docs/SCHEMA.md](docs/SCHEMA.md) | Postgres DDL + ClickHouse table + dataset versioning |
| [docs/ORCHESTRATION.md](docs/ORCHESTRATION.md) | Run FSM, ephemeral ledger, result path, weighted-fair scheduling, failure analysis |
| [docs/PLUGINS.md](docs/PLUGINS.md) | Extensibility contract (harness/scorer/loader/tool) |
| [docs/SANDBOXING.md](docs/SANDBOXING.md) | Tiered K8s isolation for agentic evals |

**Stack (chosen):** Inspect AI kernel (Pure A) · LiteLLM single-egress gateway · Ray/KubeRay ·
FastAPI control plane · Postgres metadata + ephemeral ledger · S3/MinIO artifacts ·
ClickHouse analytics · Langfuse tracing · Next.js dashboard + Superset · Terraform/Helm on
cloud Kubernetes, portable by interface.

## Prototype

[`prototype/`](prototype/) is a working single-process miniature of the spine — proves the
Inspect integration, plugin contract, ledger lifecycle, production-shaped analytics,
exactly-once concurrency, the FastAPI control plane, and a dashboard. Local stand-ins
(SQLite→Postgres, DuckDB→ClickHouse, `mockllm`→real model). See
[prototype/README.md](prototype/README.md).

```bash
python3 -m pip install --user virtualenv && python3 -m virtualenv .venv
.venv/bin/pip install -e prototype
cd prototype && PYTHONPATH=. ../.venv/bin/eval-engine run examples/capitals_qa.yaml
# dashboard:  PYTHONPATH=. ../.venv/bin/uvicorn eval_engine.api:app --port 8077
```

Status: design v0.2 (decisions locked) · Phase 0/1 prototype working.
