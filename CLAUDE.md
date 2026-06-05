# Eval Engine — orientation for Claude

A modular, distributed **LLM-evaluation platform**: run *any model × harness × dataset × scorer* at
scale, with a dashboard to author / launch / monitor / analyze. Built **around Inspect AI as the kernel**
("Pure A") — harnesses are Inspect Solvers, scorers are Inspect Scorers, `.eval` logs are the
source-of-truth artifact; our value-add is distributed orchestration + a ClickHouse analytics
projection + the dashboard.

## ▶ Start here

**`docs/PROJECT_PROGRESS.md`** is the single source of truth for *what's built vs. what's left*. New
session: open it, pick the top unchecked item in **Open v1 gaps**, build it, check it off. Anything in
`docs/FUTURE.md` is **out of scope** (deferred behind a trigger) — don't pick those up.

## Docs map

| Doc | What it covers |
|---|---|
| **`docs/PROJECT_PROGRESS.md`** | **Progress + the actionable v1 gap backlog. Start here.** |
| `DESIGN.md` | The v1 design (the target). Scale envelope, architecture, domain model, decisions. |
| `docs/DEPLOYMENT.md` | GKE bring-up tracker (milestones M0–M11), pause/resume, cluster specifics. |
| `docs/ORCHESTRATION.md` | Ledger lifecycle: claim/lease/retry/resume, commit protocol, finalize. |
| `docs/SCHEMA.md` | Postgres DDL + ClickHouse table + dataset versioning. |
| `docs/PLUGINS.md` | Harness/scorer plugin contract + registry. |
| `docs/SANDBOXING.md` | Agentic sandbox tiers (T1/T2/T3), air-gap, K8s provider. |
| `docs/SCHEDULER.md` | Two-lane admission (v1) + the deferred fair-share design. |
| `docs/FUTURE.md` | **Deferred** subsystems (out of scope) + their re-introduction triggers. |
| `docs/ALTERNATIVES.md` | Approaches evaluated and rejected (and reversed decisions). |
| `docs/DEVELOPMENT.md` | Local dev setup (Postgres + ClickHouse stand-ins, test suite). |

## Code map (`eval_engine/`)

- `api.py` — FastAPI control plane (`POST /runs`, status, results, transcript, catalog).
- `runner.py` — execute path: build Inspect Task, run, price cost, write transcript + `.eval` log.
- `worker.py` — distributed claim→execute→commit→load loop (KEDA-scaled).
- `orchestrator.py` — leader-elected admit + finalize loop.
- `control.py` — Postgres: runs + ephemeral ledger + entity registry + audit (the coordinator).
- `analytics.py` — ClickHouse: flattened per-sample projection. `db.py` exposes both + `init()`.
- `builtins.py` — built-in harnesses (`single_turn`, `agentic`) + scorers (`includes`, `match`, `llm_judge`).
- `plugins.py` — in-process plugin registry + JSON-Schema catalog. `models.py` — `RunSpec`. `datasets.py` — JSONL loader.
- `view_main.py` — patched Inspect viewer entrypoint (serves `gs://` logs behind the OIDC ingress).

Deploy: `deploy/k8s/` (manifests), `deploy/terraform/` (cluster), `deploy/Dockerfile` (one image, role
by command), `frontend/` (Next.js), `infra/` (up/down + cloud pause/resume scripts).

## Working conventions

- **Storage tier is Postgres + ClickHouse** (the only backends; no SQLite/DuckDB stand-in). Local dev
  runs them in docker via `infra/up.sh` (`docs/DEVELOPMENT.md`); the cluster uses managed/operator ones.
  The agentic sandbox still swaps by config (Docker local → k8s in-cluster).
- The user works **directly on `main`**. **Commit/push only when asked.** Don't paste secrets/DSNs.
- Cluster costs money: `infra/cloud-down.sh` pauses (system pool → 0), `infra/cloud-up.sh` resumes.
- Agentic runs use **`batch_size: 1`** (one sample per Inspect eval → one sandbox; >1 deadlocks).
- When you finish a backlog item, tick its box in `docs/PROJECT_PROGRESS.md` with a one-line note.
