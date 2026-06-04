# Eval Engine — Phase 0 Prototype

A miniature of the engine's **spine**, to de-risk the core assumptions before building out the
full system. Proves: Inspect AI integration (Pure A), the plugin contract, the result path
(Inspect log → flatten → store → query), and — against the real backends — **distributed
execution**: Ray workers fanning out over a shared Postgres ledger with exactly-once claiming
and crash resumability (`ray_executor.py`, "Distributed execution" below).

## What's real vs stubbed

| Production (DESIGN.md) | Here | Faithful because |
|---|---|---|
| Inspect on Ray | Inspect in-process | same kernel, no distribution |
| Postgres (metadata + **ephemeral ledger**) | SQLite `.data/control.db` (`runs`, `sample_tasks`, `failed_task_archive`) | same tables/lifecycle; only the claim primitive (FOR UPDATE SKIP LOCKED) changes |
| ClickHouse | DuckDB `.data/analytics.duckdb` (`sample_results`, 20 cols) | *the design's named dev path*; production-shaped (scores map, group_key, tokens/cost) |
| S3 + zstd transcripts | `.data/transcripts/<run>/<sample>.json` | object-store stand-in (no compression) |
| LiteLLM + real model | Inspect `mockllm` **(default)** *or* real **OpenRouter** model (cost from catalog price) | same `Target` swap; LiteLLM gateway stands in as catalog-priced cost |
| Ray orchestrator | single-process loop **(default)** *or* real **local Ray** workers (`ray_executor.py`) | same claim→commit→load→prune loop, now also proven across worker *processes* |
| K8s sandbox (agentic) | Inspect **Docker** sandbox, air-gapped + hardened (`sandbox/airgap-compose.yaml`) | identical Inspect `sandbox()` contract; only the provider changes (docker→k8s) — gVisor/Kata escape boundary is the one k8s-only piece |

The runner exercises the real result path: **expand ledger → claim batch → execute (Inspect)
→ commit result → batch-load to analytics → finalize (aggregate, archive failures, prune)**.

Agentic harnesses + sandboxing now run **locally** via Inspect's Docker sandbox provider (the
k8s-sandbox stand-in) — see "Agentic execution + sandboxing" below and `docs/SANDBOXING.md`.

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

Swap `model:` in the RunSpec to a real provider, drop `mock_output`, export the key. Nothing
else changes — that's the point of the model being just a `Target`. A worked OpenRouter example
ships as `examples/capitals_openrouter.yaml` (cheap Llama-3.1-8B, ~$0.000002 for the 3 samples):

```bash
../.venv/bin/pip install -e 'prototype[openrouter]'   # Inspect's openrouter/ provider needs openai
export OPENROUTER_API_KEY=sk-or-...                    # (or `set -a; source ../.env`)
PYTHONPATH=. ../.venv/bin/python -m eval_engine.cli run examples/capitals_openrouter.yaml
```

Expected: `accuracy 100%` (a real model gets all three) with **real token + cost** — vs the
mock's 33%. Cost is computed from Inspect's token usage × OpenRouter's catalog price
(`runner._cost_usd`), a stand-in for the production **LiteLLM gateway**, which is the design's
source-of-truth for per-run cost attribution + hard-cap. Other providers (`openai/gpt-4o-mini`,
`anthropic/claude-...`) work the same way; only `openrouter/*` is priced here (others → cost 0).

## Agentic execution + sandboxing

The `agentic` harness is the leap from `single_turn`: instead of one `generate()`, it runs a
**tool-use agent** (`basic_agent` with a `bash`/`python` tool) whose tool calls execute **inside
a sandbox**. The plugin contract carries it cleanly — an agentic harness returns `(solver,
sandbox)`, and the runner passes that sandbox to the Inspect `Task` (the "harness declares its
sandbox; the orchestrator provisions it" shape from `docs/SANDBOXING.md` §4/§7).

Locally the sandbox is **Inspect's Docker provider** standing in for the production **Kubernetes
sandbox provider** — the Inspect `sandbox()` contract is identical, only `sandbox: docker|k8s`
changes (same spirit as SQLite→Postgres). `sandbox/airgap-compose.yaml` maps the doc's baseline
hardening + air-gap onto Docker: **`network_mode: none`** (zero egress — can't reach Postgres, the
gateway, or IMDS), read-only rootfs, non-root, `cap_drop: ALL`, `no-new-privileges`, pid/mem caps.
What Docker *can't* give you locally is the gVisor/Kata kernel-escape boundary — that stays a k8s
concern by design.

```bash
docker pull python:3.11-slim                          # pre-pull so the air-gapped container starts
PYTHONPATH=. ../.venv/bin/python tests/test_sandbox_agentic.py   # deterministic, no API key

# real-model demo (needs OPENROUTER_API_KEY):
PYTHONPATH=. ../.venv/bin/python -m eval_engine.cli run examples/agentic_sandbox.yaml
```

The test's proof is airtight: the sandbox sets `EE_SANDBOX_SECRET`, the worker process does **not**
have it, and the secret is **never in the prompt** — so the agent reading it back via `bash` is
proof the tool ran *inside* the container. (Agentic needs a capable **native tool-caller**:
`gpt-4o-mini` scores 2/2; cheap llama-3.1-8b emits tool calls as literal text that never execute.)

## Layout

```
eval_engine/
  plugins.py    registry + @harness/@scorer (prototype of docs/PLUGINS.md)
  builtins.py   single_turn + agentic (tool-use, sandbox) harnesses; includes/match/llm_judge scorers
  datasets.py   JSONL → Inspect dataset (+ content hash, à la SCHEMA §0)
  models.py     RunSpec (SCHEMA §1.5)
  control.py    SQLite: runs + ephemeral sample-task ledger + failure archive (SCHEMA §1);
                atomic UPDATE..RETURNING claim w/ lease reclaim (SKIP-LOCKED analogue)
  control_pg.py Postgres backend: same interface, REAL FOR UPDATE SKIP LOCKED claim
  analytics.py  DuckDB: production-shaped sample_results + slice queries (SCHEMA §2)
  analytics_ch.py  ClickHouse backend: ReplacingMergeTree(attempt), monthly partitions, TTL
  db.py         backend selector (EVAL_ENGINE_BACKEND=sqlite|postgres)
  runner.py     result-path lifecycle, split launch()/execute()/_finalize() (ORCHESTRATION §4–§10)
  ray_executor.py  DISTRIBUTED executor: N Ray workers over the shared Postgres ledger (Phase 2)
  api.py        FastAPI control plane: POST /runs (bg execute), GET status/results/catalog
  cli.py        run | report | runs | catalog | ledger
tests/
  test_concurrency.py      exactly-once + lease-reclaim (SQLite)
  test_concurrency_pg.py   exactly-once + lease-reclaim (Postgres SKIP LOCKED)
  test_distributed_ray.py  exactly-once + crash-reclaim across Ray worker processes
  test_sandbox_agentic.py  agentic harness + tool exec inside an air-gapped Docker sandbox
infra/          up.sh / down.sh — docker Postgres + ClickHouse
sandbox/        airgap-compose.yaml — hardened, air-gapped agentic sandbox (k8s-sandbox stand-in)
examples/       qa.jsonl + capitals_qa.yaml + capitals_openrouter.yaml + sandbox_qa.jsonl + agentic_sandbox.yaml
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

## Distributed execution (Ray) — the Phase 2 leap

`ray_executor.py` replaces the single-process loop with **N Ray workers, each running the
identical claim→execute→commit→batch-load loop against the same Postgres ledger**. The workers
never coordinate with each other — the ledger (`FOR UPDATE SKIP LOCKED`) is the sole
coordinator, so distribution is just *"run N copies of a loop already proven exactly-once."*
Needs the **postgres** backend (Ray workers are separate processes; DuckDB can't take
concurrent multi-process writes, ClickHouse + Postgres can). A local `ray.init()` proves the
*model*; KubeRay is deployment, a separate concern.

```bash
../.venv/bin/pip install -e 'prototype[postgres,ray]'
bash infra/up.sh
EVAL_ENGINE_BACKEND=postgres PYTHONPATH=. ../.venv/bin/python tests/test_distributed_ray.py
```

Proves two things the distributed system rides on:
- **exactly-once across worker *processes*** — every sample lands in ClickHouse exactly once,
  ledger prunes to 0 (not just across threads, as the concurrency test shows);
- **crash resumability** — a worker that hard-crashes holding a claimed batch has its tasks
  reclaimed by a survivor once the lease expires, and the run still completes — no orchestrator,
  the ledger recovers itself.

```python
from eval_engine.ray_executor import run_distributed   # launch + distributed execute
run_id = run_distributed(spec, n_workers=4)
```

## MCP servers (for Claude Code)

A project `.mcp.json` (root, **gitignored** — it holds a machine-specific venv path + a dev
credential) wires Claude Code into the running dev backends, **read-only**, so it can inspect
the live ledger and run analytics slices without hand-written scripts:

- `eval-pg` — `@modelcontextprotocol/server-postgres` (via npx) over a dedicated **read-only
  role** `ee_ro` (not the owner; the server also wraps every query in a `READ ONLY` txn).
- `eval-clickhouse` — `mcp-clickhouse` (in the venv); `run_select_query` executes with
  `readonly=1`.

Recreate after `infra/up.sh`:

```bash
# read-only Postgres role (idempotent)
docker exec -i ee-postgres psql -U evalengine -d evalengine <<'SQL'
DROP ROLE IF EXISTS ee_ro;
CREATE ROLE ee_ro LOGIN PASSWORD 'ee_ro';
GRANT CONNECT ON DATABASE evalengine TO ee_ro;
GRANT USAGE ON SCHEMA public TO ee_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ee_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO ee_ro;
SQL

../.venv/bin/pip install mcp-clickhouse   # ClickHouse MCP server
```

`.mcp.json`:

```json
{
  "mcpServers": {
    "eval-pg": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-postgres",
               "postgresql://ee_ro:ee_ro@localhost:5433/evalengine"]
    },
    "eval-clickhouse": {
      "command": "<repo>/.venv/bin/mcp-clickhouse",
      "env": {"CLICKHOUSE_HOST": "localhost", "CLICKHOUSE_PORT": "8123",
              "CLICKHOUSE_USER": "default", "CLICKHOUSE_PASSWORD": "", "CLICKHOUSE_SECURE": "false"}
    }
  }
}
```

Claude Code prompts to approve project MCP servers on next start (`/mcp` to manage). The
servers need the `infra/up.sh` containers running.
