# Refactoring findings

A read-through of `eval_engine/` (≈4.9k LOC of Python) looking for duplication, fragile coupling,
dead code, and consistency drift. Findings are grouped by theme and ordered roughly by payoff.
Each item cites `file:line` and proposes a concrete change. Nothing here changes behaviour by
itself — these are cleanups, not feature work or v1-gap items.

The codebase is in good shape overall: small modules, heavy doc-comments, clear two-plane
separation. The issues below are mostly *drift* — places where the schema/queries evolved but a
caller, a duplicated constant, or a stale CLI didn't follow.

---

## 1. Correctness bugs surfaced while reading (fix first)

These aren't style — they're latent breakage from column-count drift. The CLI has no tests
(`tests/` has `test_api`, `test_ops`, … but no `test_cli`), which is why they went unnoticed.

### 1a. `cli.cmd_runs` unpacks the wrong number of columns — `eval-engine runs` is broken
`eval_engine/cli.py:35`
```python
for rid, ev, model, acc, total, created in rows:   # expects 6 columns
```
`control.list_runs()` (`control.py:296`) returns **11** columns
(`id, eval_id, eval_version, model, accuracy, total, cost_usd, created_at, created_by, status, sweep`).
This raises `ValueError: too many values to unpack` the moment any run exists.

### 1b. `cli._print_report` unpacks `analytics.samples()` wrong — `eval-engine report` is broken
`eval_engine/cli.py:62`
```python
for sid, p, gk, score, uri in analytics.samples(run_id):   # expects 5 columns
```
`analytics.samples()` (`analytics.py:120`) returns **8**
(`sample_id, passed, group_key, primary_score, transcript_uri, tokens, latency_ms, error_type`).
Same crash. (`api.py:232` unpacks all 8 correctly — only the CLI drifted.)

**Fix:** update the CLI unpacking to match, and add a smoke test
(`tests/unit/test_cli.py`) that runs `cmd_runs`/`cmd_report` against a tiny seeded run so the
next column change fails in CI instead of in a user's terminal. This also motivates §3.

---

## 2. Positional column lists kept in sync by hand (the root cause of §1)

Several places hand-maintain "this list of column names must match that SQL SELECT", with comments
admitting the coupling. Any column add/reorder silently misaligns the dict that downstream code and
the frontend read.

- `control.RUN_COLS` (`control.py:307`) is a SQL string; `api.get_run` (`api.py:164`) re-declares the
  *same* columns as a Python list to `zip`. Comment at `control.py:305` and `api.py:163` both say
  "keep in sync."
- `control.list_runs()` (`control.py:296`) SELECT order must match `api.list_runs` cols
  (`api.py:153`) — comment at `control.py:297` says so. The CLI (§1a) is the third consumer that
  drifted.
- `analytics._COLUMNS` (`analytics.py:76`) is the insert column order; the two insert-tuple builders
  in `runner` (§4) must produce values in exactly that order with no checking.

**Fix:** return mappings, not tuples, from the data layer. `psycopg` supports
`row_factory=dict_row`; `clickhouse_connect` can return column names. Have `control.list_runs`,
`control.get_run`, `analytics.samples`, `analytics.run_summary` return `dict`s keyed by column, and
delete the parallel `cols = [...]` lists in `api.py` and the unpacking in `cli.py`. This eliminates
the entire class of bug in §1 and the "keep in sync" comments.

---

## 3. The shared execute path lives behind `runner._private` functions

`worker.py` and `orchestrator.py` both reach into `runner`'s underscore-prefixed internals:

- `worker.py:62` calls `runner._execute_batch`
- `worker.py:87`, `orchestrator` (via) call `runner._commit_batch`
- `orchestrator.py:82` calls `runner._enforce_budget`
- `orchestrator.py:93` calls `runner._batch_load`
- `orchestrator.py:94` calls `runner._finalize`

These are effectively the public distributed-execution API, but the `_` prefix says "private." A
reader can't tell the contract surface from genuinely-internal helpers (`_hash01`, `_score_value`).

**Fix:** promote the cross-module ones to public names (`execute_batch`, `commit_batch`,
`enforce_budget`, `batch_load`, `finalize`) and list them in `runner.__all__` alongside `run`,
`launch`, `execute`. Optionally move the batch-execution core into a `runner/` package or an
`execution.py` so `runner.py` (455 lines, the largest non-`control` file) stops carrying pricing,
transcript retention, model resolution, *and* the loop all at once.

---

## 4. The analytics insert tuple is assembled twice, identically

`runner._commit_batch` (`runner.py:207`) and `runner._batch_load` (`runner.py:358`) each build a
20-field tuple in `analytics._COLUMNS` order, by hand, with the same `provider/model_id/harness_type/
group_key/passed/.../finished_at` layout. Two copies of a positional 20-tuple is a maintenance trap
(add a column to ClickHouse → must edit both, in order, or get a silent shift — see §2).

**Fix:** one helper, e.g. `analytics.row(run_id, spec, sid, fields) -> tuple` (or a small dataclass
that knows its own column order), called from both sites. Pairs naturally with the dict-returning
refactor in §2.

---

## 5. Leader-election loop duplicated between `orchestrator` and `training`

`orchestrator.main` (`orchestrator.py:116`) and `training.main` (`training.py:406`) are near-identical:
contend for an advisory lock, `reap_stale_leader` on contention, heartbeat as standby, then a
`while leader_alive(): tick(); sleep()` loop, plus a `_graceful_shutdown` that calls
`release_leader` + `sys.exit(0)`. Only the `LEADER_KEY`, tick function, and log prefix differ. The
`mock_trainer` likely has a third copy of the loop scaffolding.

**Fix:** extract `control.run_as_leader(key, tick, *, tick_seconds, stale_seconds, name)` (or a small
`leader.py`) that owns acquire → standby → loop → SIGTERM-release. Both entrypoints shrink to
`run_as_leader(LEADER_KEY, tick)`. This also fixes the constant duplication in §6 for
`STALE_LEADER_SECONDS`.

---

## 6. Config read from `os.environ` ad hoc, with duplicated constants

~45 `os.environ.get("EVAL_ENGINE_*")` reads are scattered across 9 modules. Several constants are
read in **two** places with the default repeated, so they can silently diverge:

- `EVAL_ENGINE_GLOBAL_MAX_RUNNING` (default `50`) — `orchestrator.py:40` **and** `ops.py:40`
- `EVAL_ENGINE_INTERACTIVE_RESERVE` (default `12`) — `orchestrator.py:41` **and** `ops.py:41`
- `EVAL_ENGINE_ORCH_TICK` (default `2.0`) — `orchestrator.py:33` **and** `ops.py:38`
- `EVAL_ENGINE_WORKER_POLL` (default `1.0`) — `worker.py:23` **and** `ops.py:39`
- `EVAL_ENGINE_STALE_LEADER_SECONDS` (default `20`) — `orchestrator.py:35` **and** `training.py:40`

`ops.py` re-reads the scheduler knobs only to *display* them; if someone changes the orchestrator
default and forgets `ops.py`, the dashboard reports a cap the system isn't using.

**Fix:** a single `config.py` with the typed constants (one definition per env var, one default),
imported where needed. Doesn't need to be heavy (a module of module-level constants, or a frozen
`Settings` dataclass). Removes the drift risk and gives one place to document every tunable.

---

## 7. Dead / unused code

- `runner._model_for` (`runner.py:185`) is defined and never called — `_execute_batch` inlines
  `_resolve_model` + `_build_model` instead (`runner.py:288`). Delete it.
- `_resolve_model` is then called twice per batch in the execute path (once at `runner.py:288`, and
  it's also what `_model_for` wrapped). Not a correctness issue, but the helper that existed to
  centralize it is the dead one. Either use `_model_for` or remove it; don't keep both.

---

## 8. Two different lazy-init patterns for the two DB clients

- `control.py` uses `_conn()` (thread-local) + a module `init()` guarded by `_init_done`
  (`control.py:142–210`).
- `analytics.py` uses `_c()` → `_c_inited()`, a two-function dance where `_c` opens the client and
  `_c_inited` runs the DDL (`analytics.py:48–69`).

Same job ("connect once, ensure schema once"), two shapes. The `analytics` split in particular
(`_c` always calls `_c_inited`, which gates on `_init_done`) is harder to follow than `control`'s.

**Fix:** align them — either both `_conn()/init()` or both a single `client()` that ensures schema.
Minor, but these are the two files every other module depends on, so consistency pays off.

### 8a. `_init_done` is module-global, not connection-scoped (latent, both modules)
`control._init_done` (`control.py:143`) and `analytics._init_done` (`analytics.py:45`) are process
globals. If a connection drops and `_conn()` reopens it (`control.py:148`), `init()` won't re-run —
fine today because schema is `IF NOT EXISTS`, but worth a comment, or tie the flag to the connection
object so a fresh connection re-ensures.

---

## 9. Repeated boilerplate in `builtins` scorers

The four custom scorers — `numeric_answer` (`builtins.py:312`), `code_exec` (`:367`), `ifeval`
(`:403`), `swe_bench_scorer` (`:433`) — each repeat the same wrapper:
```python
@inspect_scorer(metrics=[accuracy(), stderr()], name="...")
def _factory() -> Scorer:
    async def score(state, target) -> Score: ...
    return score
return _factory()
```
The `metrics=[accuracy(), stderr()]` + `_factory` + `return _factory()` shell is identical four
times; only the inner `score` body differs.

**Fix:** a small helper `simple_scorer(name)(async_fn)` that wraps an `async score(state, target)`
in the standard metrics/factory shell. Each scorer becomes the `score` body plus one decorator line.

### 9a. Sandbox config fields duplicated across harness configs
`CodeGenerationConfig` (`builtins.py:74`) and `AgenticConfig` (`builtins.py:166`) both declare
`sandbox` / `compose_file` / `k8s_values`. Worse, `code_generation` defaults them from the module
constants `_DEFAULT_COMPOSE` / `_DEFAULT_K8S_VALUES` (`builtins.py:30`) while `agentic` **re-hardcodes
the same path strings inline** (`builtins.py:169–170`) instead of reusing the constants — so the
"default sandbox compose" path now lives in three spots. Extract a shared `SandboxConfig` mixin (or
at least make `agentic` use the constants).

---

## 10. Smaller consistency / naming nits

- **`api.get_model` endpoint name** (`api.py:341`) shadows the well-known `inspect_ai.model.get_model`
  conceptually; a reader grep-ing for the Inspect call hits the route. Consider `get_model_entity`.
- **`auth_email` dependency returns into a param named `x_auth_request_email`** (`api.py:115`, `:136`,
  etc.) even though it now prefers `X-Forwarded-Email` (commit `f64a828`). The param name is stale;
  rename to `actor`/`email` so it reads as "the resolved identity," not "this specific header."
- **`swebench` has three near-identical pytest parsers** (`_parse_pytest`, `_parse_pytest_v2`,
  `_parse_pytest_options`, `swebench.py:88–140`) sharing the `STATUSES` prefix-scan + `FAILED " - "`
  fixup. They're vendored from SWE-bench so faithfulness matters, but the shared prefix-scan could be
  one helper with per-variant line-normalization hooks. Low priority (vendored = keep close to source).
- **`_dsn_host` URI fallback** (`control.py:33`) re-imports `urlparse` inside the function each call;
  move to module scope. Trivial.

---

## Suggested order of attack

1. **§1 + §3 tests** — fix the broken CLI and add the smoke test that would have caught it.
2. **§2 + §4** — switch the data layer to dict rows; deletes the parallel column lists and the
   duplicated insert tuple in one sweep (and is what makes §1 not recur).
3. **§6** — central `config.py`; removes the diverging-default risk.
4. **§5** — shared leader loop; collapses two ~30-line `main()`s.
5. **§7, §8, §9, §10** — opportunistic cleanups as those files are touched.

None of these are in `docs/PROJECT_PROGRESS.md`'s v1-gap backlog, so schedule them around feature
work rather than ahead of it — except §1, which is a user-visible break.
