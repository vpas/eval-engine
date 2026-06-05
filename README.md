# eval-engine

A modular, distributed engine for **model evaluations** at scale, plus a web dashboard to
author, launch, monitor, and analyze runs. Composability is the core principle: *model*,
*harness*, *dataset*, and *scorer* are independent blocks, and any valid combination works —
across QA, multi-turn agentic, LLM-as-judge, and custom-code scoring.

## Design docs

The docs describe the **current (v1) design**. Approaches considered and dropped are in
[docs/ALTERNATIVES.md](docs/ALTERNATIVES.md); deferred/future work (with triggers) is in
[docs/FUTURE.md](docs/FUTURE.md); the design-review history is archived under
[docs/design_review_history/](docs/design_review_history/).

| Doc | Contents |
|---|---|
| [DESIGN.md](DESIGN.md) | Architecture, scale envelope, component decisions, reproducibility |
| [docs/SCHEMA.md](docs/SCHEMA.md) | Postgres DDL + ClickHouse table + dataset versioning |
| [docs/ORCHESTRATION.md](docs/ORCHESTRATION.md) | Run FSM, ephemeral ledger, commit protocol, budget, failure analysis |
| [docs/SCHEDULER.md](docs/SCHEDULER.md) | Two-lane admission + per-run concurrency cap |
| [docs/PLUGINS.md](docs/PLUGINS.md) | Extensibility contract (harness/scorer/loader/tool) |
| [docs/SANDBOXING.md](docs/SANDBOXING.md) | Tiered K8s isolation for agentic evals |
| [docs/ALTERNATIVES.md](docs/ALTERNATIVES.md) | Considered & rejected; decisions reversed |
| [docs/FUTURE.md](docs/FUTURE.md) | Deferred subsystems & roadmap (with re-introduction triggers) |

**Stack (chosen):** Inspect AI kernel (Pure A) · LiteLLM gateway (all traffic) · K8s Deployment +
KEDA · FastAPI control plane · Postgres metadata + ephemeral ledger · S3/MinIO artifacts ·
ClickHouse analytics · Next.js dashboard + embedded Inspect viewer + canned CH views · Terraform/Helm
on cloud Kubernetes, portable by interface.

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

Status: design v1 (current; reviewed three rounds) · Phase 0/1 prototype working.
