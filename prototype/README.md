# Eval Engine — Phase 0 Prototype

A single-process miniature of the engine's **spine**, to de-risk the core assumptions before
building the distributed system. Proves: Inspect AI integration (Pure A), the plugin contract,
and the result path (Inspect log → flatten → store → query).

## What's real vs stubbed

| Production (DESIGN.md) | Here | Faithful because |
|---|---|---|
| Inspect on Ray | Inspect in-process | same kernel, no distribution |
| Postgres (metadata + **ephemeral ledger**) | SQLite `.data/control.db` (`runs`, `sample_tasks`, `failed_task_archive`) | same tables/lifecycle; only the claim primitive (FOR UPDATE SKIP LOCKED) changes |
| ClickHouse | DuckDB `.data/analytics.duckdb` (`sample_results`, 20 cols) | *the design's named dev path*; production-shaped (scores map, group_key, tokens/cost) |
| S3 + zstd transcripts | `.data/transcripts/<run>/<sample>.json` | object-store stand-in (no compression) |
| LiteLLM + real model | Inspect `mockllm` (fixed output) | zero cost/keys; proves plumbing |
| Ray orchestrator | single-process claim→commit→load→prune loop | exact lifecycle (ORCHESTRATION §4–§10), not scale |

The runner exercises the real result path: **expand ledger → claim batch → execute (Inspect)
→ commit result → batch-load to analytics → finalize (aggregate, archive failures, prune)**.

Agentic harnesses + sandboxing are **out of this prototype** (need Docker/k8s; see
`docs/SANDBOXING.md`).

## Setup

```bash
# from repo root — virtualenv (no sudo needed; python3.10-venv is unavailable here)
python3 -m pip install --user virtualenv
python3 -m virtualenv .venv
.venv/bin/pip install inspect-ai duckdb pydantic pyyaml
```

## Run

```bash
cd prototype
PYTHONPATH=. ../.venv/bin/python -m eval_engine.cli catalog                  # list plugins
PYTHONPATH=. ../.venv/bin/python -m eval_engine.cli run examples/capitals_qa.yaml
PYTHONPATH=. ../.venv/bin/python -m eval_engine.cli runs                     # list past runs
PYTHONPATH=. ../.venv/bin/python -m eval_engine.cli report <run_id>
```

Expected: `accuracy 33%` (mock always answers "Paris" → q1 passes, q2/q3 fail).

## Using a real model

Swap `model:` in the RunSpec to e.g. `openai/gpt-4o-mini` or `anthropic/claude-...`, drop
`mock_output`, and export the provider key (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`). Nothing
else changes — that's the point of the model being just a `Target`.

## Layout

```
eval_engine/
  plugins.py    registry + @harness/@scorer (prototype of docs/PLUGINS.md)
  builtins.py   single_turn harness; includes/match/llm_judge scorers
  datasets.py   JSONL → Inspect dataset (+ content hash, à la SCHEMA §0)
  models.py     RunSpec (SCHEMA §1.5)
  control.py    SQLite: runs + ephemeral sample-task ledger + failure archive (SCHEMA §1);
                atomic UPDATE..RETURNING claim w/ lease reclaim (SKIP-LOCKED analogue)
  control_pg.py Postgres backend: same interface, REAL FOR UPDATE SKIP LOCKED claim
  analytics.py  DuckDB: production-shaped sample_results + slice queries (SCHEMA §2)
  analytics_ch.py  ClickHouse backend: ReplacingMergeTree(attempt), monthly partitions, TTL
  db.py         backend selector (EVAL_ENGINE_BACKEND=sqlite|postgres)
  runner.py     result-path lifecycle, split launch()/execute() (ORCHESTRATION §4–§10)
  api.py        FastAPI control plane: POST /runs (bg execute), GET status/results/catalog
  cli.py        run | report | runs | catalog | ledger
tests/
  test_concurrency.py      exactly-once + lease-reclaim (SQLite)
  test_concurrency_pg.py   exactly-once + lease-reclaim (Postgres SKIP LOCKED)
infra/          up.sh / down.sh — docker Postgres + ClickHouse
examples/       qa.jsonl + capitals_qa.yaml
```

## Control plane API (Phase 1 slice)

```bash
PYTHONPATH=. ../.venv/bin/uvicorn eval_engine.api:app --port 8077   # http://localhost:8077/docs
curl -s localhost:8077/catalog
curl -s -X POST localhost:8077/runs -H 'content-type: application/json' -d @examples/run.json
curl -s localhost:8077/runs/<id>           # live progress polled from the ledger
curl -s localhost:8077/runs/<id>/results   # summary + by_category + samples
```

`POST /runs` validates plugins, creates the run + expands the ledger, returns a `run_id`
immediately (202), and executes in the background — the two-plane split in miniature.

## Dashboard

The FastAPI app serves a self-contained single-page UI at **http://localhost:8077/** —
launch form (harness/scorer dropdowns from `/catalog`), auto-refreshing runs list, and a
results panel (accuracy/tokens/cost, accuracy-by-category, per-sample table) with a live
progress bar driven by the ledger. Vanilla HTML/JS, no build step.

> Prototype stand-in: production uses **Next.js + Superset** (DESIGN §6.6). This single page
> proves the dashboard *function* (launch / list / drill-in) with zero build infra, same as
> SQLite stands in for Postgres.

## Concurrency test (the claim/idempotency correctness)

```bash
PYTHONPATH=. ../.venv/bin/python tests/test_concurrency.py        # SQLite
PYTHONPATH=. ../.venv/bin/python tests/test_concurrency_pg.py     # Postgres (real SKIP LOCKED)
```

Proves exactly-once claim (no double-claim, none dropped) and lease-based reclaim of tasks
abandoned by a "crashed" worker. SQLite uses an atomic `UPDATE..RETURNING`; Postgres uses the
real `FOR UPDATE SKIP LOCKED` — where all N workers claim *in parallel* (2000 samples / 12
workers, exactly-once, ~3.8k samples/s).

## Real backends (Postgres + ClickHouse)

The SQLite/DuckDB stand-ins swap for the real backends via one env var — same interface
(`db.py` selector), so nothing else changes. The only behavioural difference is the claim
becomes a true `FOR UPDATE SKIP LOCKED`.

```bash
../.venv/bin/pip install -e 'prototype[postgres]'   # psycopg + clickhouse-connect
bash infra/up.sh                                    # docker Postgres :5433 + ClickHouse :8123
EVAL_ENGINE_BACKEND=postgres PYTHONPATH=. ../.venv/bin/eval-engine run examples/capitals_qa.yaml
bash infra/down.sh                                  # teardown
```

Config: `EVAL_ENGINE_PG_DSN`, `EVAL_ENGINE_CH_HOST/PORT/USER/PASSWORD`. ClickHouse table uses
the production engine (`ReplacingMergeTree(attempt)`, monthly partitions, 12-month TTL).
