# Eval Engine — Development Guide

How to run the engine **locally**. The `eval_engine/` package is the spine that deploys to GKE (see
`docs/DEPLOYMENT.md`); locally it runs single-process against the **same backends as production**
(Postgres + ClickHouse in docker, `infra/up.sh`). It proves: Inspect AI integration (Pure A), the
plugin contract, the result path (Inspect log → flatten → store → query), and exactly-once claiming
with lease-based crash recovery (`tests/test_concurrency.py`).

## What's real vs stubbed

The **storage tier is the real thing** — Postgres + ClickHouse, just running in local docker. What's
stubbed locally is distribution, the model, and the sandbox runtime:

| Production (DESIGN.md) | Local | Faithful because |
|---|---|---|
| Postgres (metadata + **ephemeral ledger**) | **same** — docker Postgres (`infra/up.sh`) | identical engine + the real `FOR UPDATE SKIP LOCKED` claim |
| ClickHouse (analytics) | **same** — docker ClickHouse (`infra/up.sh`) | identical engine (`ReplacingMergeTree`, partitions, TTL) |
| Inspect on K8s workers | Inspect in-process | same kernel, no distribution |
| Worker/Orchestrator split (K8s) | single-process `runner.run()` loop | same claim→commit→load→prune lifecycle; the distributed split is `eval_engine.worker`/`orchestrator` (see `docs/DEPLOYMENT.md` M3) |
| S3 + zstd transcripts | `.data/transcripts/<run>/<sample>.json` (when no GCS bucket set) | object-store stand-in (no compression) |
| LiteLLM + real model | Inspect `mockllm` **(default)** *or* real **OpenRouter** model (cost from catalog price) | same `Target` swap; LiteLLM gateway stands in as catalog-priced cost |
| K8s sandbox (agentic) | Inspect **Docker** sandbox, air-gapped + hardened (`deploy/sandbox/airgap-compose.yaml`) | identical Inspect `sandbox()` contract; only the provider changes (docker→k8s) — gVisor/Kata escape boundary is the one k8s-only piece |

The runner exercises the real result path: **expand ledger → claim batch → execute (Inspect)
→ commit result → batch-load to analytics → finalize (aggregate, archive failures, prune)**.

Agentic harnesses + sandboxing now run **locally** via Inspect's Docker sandbox provider (the
k8s-sandbox stand-in) — see "Agentic execution + sandboxing" below and `docs/SANDBOXING.md`.

## Setup

```bash
# from repo root — virtualenv (no sudo needed; python3.10-venv is unavailable here)
python3 -m pip install --user virtualenv
python3 -m virtualenv .venv
.venv/bin/pip install -e .          # base deps incl. psycopg + clickhouse-connect (the backends)
bash infra/up.sh                    # docker Postgres :5433 + ClickHouse :8123 (required)
```

There is no in-memory/SQLite stand-in — the engine talks to Postgres + ClickHouse directly, so
`infra/up.sh` must be running. Connection defaults point at the local docker stack
(`EVAL_ENGINE_PG_DSN`, `EVAL_ENGINE_CH_HOST/PORT/USER/PASSWORD` override them).

## Run

```bash
.venv/bin/python -m eval_engine.cli catalog                  # list plugins (no DB needed)
.venv/bin/python -m eval_engine.cli run examples/capitals_qa.yaml
.venv/bin/python -m eval_engine.cli runs                     # list past runs
.venv/bin/python -m eval_engine.cli report <run_id>
```

Expected: `accuracy 33%` (mock always answers "Paris" → q1 passes, q2/q3 fail).

## Using a real model

Swap `model:` in the RunSpec to a real provider, drop `mock_output`, export the key. Nothing
else changes — that's the point of the model being just a `Target`. A worked OpenRouter example
ships as `examples/capitals_openrouter.yaml` (cheap Llama-3.1-8B, ~$0.000002 for the 3 samples):

```bash
.venv/bin/pip install -e '.[openrouter]'   # Inspect's openrouter/ provider needs openai
export OPENROUTER_API_KEY=sk-or-...                    # (or `set -a; source .env`)
.venv/bin/python -m eval_engine.cli run examples/capitals_openrouter.yaml
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
changes (a config choice). `deploy/sandbox/airgap-compose.yaml` maps the doc's baseline
hardening + air-gap onto Docker: **`network_mode: none`** (zero egress — can't reach Postgres, the
gateway, or IMDS), read-only rootfs, non-root, `cap_drop: ALL`, `no-new-privileges`, pid/mem caps.
What Docker *can't* give you locally is the gVisor/Kata kernel-escape boundary — that stays a k8s
concern by design.

```bash
docker pull python:3.11-slim                          # pre-pull so the air-gapped container starts
.venv/bin/python tests/test_sandbox_agentic.py   # deterministic, no API key

# real-model demo (needs OPENROUTER_API_KEY):
.venv/bin/python -m eval_engine.cli run examples/agentic_sandbox.yaml
```

The test's proof is airtight: the sandbox sets `EE_SANDBOX_SECRET`, the worker process does **not**
have it, and the secret is **never in the prompt** — so the agent reading it back via `bash` is
proof the tool ran *inside* the container. (Agentic needs a capable **native tool-caller**:
`gpt-4o-mini` scores 2/2; cheap llama-3.1-8b emits tool calls as literal text that never execute.)

## Layout

```
eval_engine/
  plugins.py    registry + @harness/@scorer (implements docs/PLUGINS.md)
  builtins.py   harnesses (single_turn / multiple_choice / agentic) + scorers (includes/match/choice/llm_judge)
  datasets.py   JSONL → Inspect dataset (+ content-addressed snapshot, SCHEMA §0/§13)
  models.py     RunSpec + registered-entity specs (Dataset/Eval/Model) (SCHEMA §1.5, §7)
  control.py    Postgres: runs + ephemeral sample-task ledger + failure archive + entity registry +
                audit log (SCHEMA §1); REAL FOR UPDATE SKIP LOCKED claim w/ lease reclaim
  analytics.py  ClickHouse: ReplacingMergeTree(attempt) sample_results + slice queries (SCHEMA §2)
  db.py         storage-tier entrypoint — exposes control + analytics + init()
  runner.py     result-path lifecycle, split launch()/execute()/_finalize() (ORCHESTRATION §4–§10)
  worker.py / orchestrator.py  the distributed split: claim loop / admit+finalize (leader-elected)
  api.py        FastAPI control plane: POST /runs, entity CRUD, rerun, GET status/results/catalog/audit
  cli.py        run | report | runs | catalog | ledger
tests/                  pytest, layered (conftest.py = fixtures + per-test truncation isolation)
  unit/               pure logic — pricing/Wilson-CI, lane classify, plugins, models, datasets (no backends)
  integration/        test_ledger.py (claim/lease/retry/budget/cap) + test_registry.py (entities/snapshot/audit)
  e2e/                test_spine.py (launch→admit→drain→finalize) + test_sandbox_agentic.py (agentic sandbox)
infra/              up.sh / down.sh — docker Postgres + ClickHouse (the backends)
deploy/sandbox/     airgap-compose.yaml — hardened, air-gapped agentic sandbox (k8s-sandbox stand-in)
examples/           qa.jsonl + capitals_qa.yaml + capitals_openrouter.yaml + sandbox_qa.jsonl + agentic_sandbox.yaml
```

## Control plane API (Phase 1 slice)

```bash
.venv/bin/uvicorn eval_engine.api:app --port 8077   # http://localhost:8077/docs
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

> Prototype stand-in: production uses **Next.js + embedded Inspect viewer + canned ClickHouse
> views** (DESIGN §6, and the `frontend/` app deployed on GKE). This single page proves the
> dashboard *function* (launch / list / drill-in) with zero build infra.

## Tests

**pytest**, layered (`tests/{unit,integration,e2e}/`) so a newcomer sees the shape at a glance:

| Layer | What | Needs |
|---|---|---|
| `unit` | pure logic — pricing, Wilson CI, lane classification, plugins, models, dataset parsing | nothing (fast) |
| `integration` | one module vs its real backend in isolation — the **ledger** (claim/lease/retry/budget/cap/live-rollup) and the **registry** (entities/snapshot/audit) | Postgres + ClickHouse (`infra/up.sh`) |
| `e2e` | the full **spine** (launch → admit → drain → finalize) + the agentic Docker sandbox | backends + docker |

```bash
make install            # pip install -e '.[test,openrouter]'
make test-unit          # fast, no backends
make test-int           # integration (auto-starts infra/up.sh)
make test               # everything
# or directly:
.venv/bin/pytest -m unit            # by marker (auto-applied from the dir)
.venv/bin/pytest tests/integration/test_ledger.py
```

Isolation is automatic: a root-`conftest.py` fixture TRUNCATEs the control tables before each
integration/e2e test, so tests never share state. The ledger test proves exactly-once claim (no
double-claim, none dropped) + lease reclaim via the real `FOR UPDATE SKIP LOCKED` where all N workers
claim *in parallel* (2000 samples / 12 workers, ~3.8k samples/s).

## Distributed execution (K8s)

The single-process `runner.run()` loop fans out, in production, to **N worker pods each running the
identical claim→execute→commit→load loop against the same Postgres ledger**, plus a single
**orchestrator** that admits/expands/reconciles/finalizes. Workers never coordinate with each other —
the ledger (`FOR UPDATE SKIP LOCKED`) is the sole coordinator, so distribution is just *"run N copies
of a loop already proven exactly-once"* (`tests/test_concurrency.py`: 2000 samples / 12 parallel
workers, exactly-once, ~3.8k samples/s). The `eval_engine.worker` / `eval_engine.orchestrator`
entrypoints and their KEDA-scaled GKE Deployments are tracked in **`docs/DEPLOYMENT.md`** (M3/M5).

(An earlier local Ray prototype proved the cross-process exactly-once + crash-reclaim model; Ray was
dropped from the design — the ledger is the only coordinator needed — see `docs/ALTERNATIVES.md` §2.)

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

.venv/bin/pip install mcp-clickhouse   # ClickHouse MCP server
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
