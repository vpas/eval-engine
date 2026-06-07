# Code review findings — 2026-06-07

A general pass over `eval_engine/` for dead code, inconsistencies, and naming/clarity smells. The
clear, safe improvements were fixed directly (see **Fixed in this pass**); everything below the line is
a judgment call left here for review rather than changed blind.

Scope: all of `eval_engine/*.py` read in full except `control.py` (947 lines — skimmed, its connection
layer + ledger API are exercised heavily by the integration suite). Mechanical checks (unused imports,
unreferenced top-level functions) were done with a stdlib-AST scan since no linter is installed.

---

## Fixed in this pass (clear, no behavior change)

1. **`cli.py` — f-string with no placeholders.** `print(f"\n  accuracy by category …")` → plain
   string (would be `F541` under ruff). Identical output.
2. **`swebench.py` — stale function name in the module docstring.** It described
   `resolved(test_output, …)`; the function is `grade(log, repo, …)` and `resolved` is now a key in
   its returned dict. Docstring corrected.
3. **`training.py` — dead read in `discover()`.** `run_json = SOURCE.read_run(source)` was assigned
   and never used (a wasted object-storage read every discovery tick). The docstring claimed it
   "re-reads run.json to … pick up a terminal trainer status," but that actually happens in
   `_maybe_finalize`. Removed the read and corrected the docstring. Verified unused; covered by the
   `test_training_monitor` integration tests.

---

## For review (not auto-fixed)

### 1. Redis probe can never report `degraded` — likely a real bug
`ops.py` `probe_redis()`:
```python
status = "ok" if role == "master" or slaves >= 0 else "degraded"
```
`slaves` is `connected_slaves`, which is always `>= 0`, so the whole condition is a tautology — the
`"degraded"` branch is unreachable and Redis always reports `ok` (even with no master link / a broken
replica). The intended health rule isn't obvious from the code, hence not auto-fixed. Likely intent:
```python
link_ok = info.get("master_link_status", "up") == "up"   # replicas report this; master omits it
status = "ok" if role == "master" or link_ok else "degraded"
```
**Action:** decide what "degraded Redis" should mean here and replace the tautology.

### 2. Dead function: `analytics.compare_models_by_category()`
Defined (`analytics.py`) but referenced nowhere — not in modules, tests, frontend, or docs. The name
maps to the planned **Compare view** (referenced in `api.py`/`training.py` comments), so it's probably
a stub for unbuilt UI rather than an accident.
**Action:** wire it into the Compare view when that lands, or delete it until then. (Left in place — I
didn't want to delete a likely-intended stub.)

### 3. Dead function: `control.mark_failed()`
`control.py` `mark_failed(run_id, sample_id, error_type)` is never called in code — the terminal-fail
path is the inline `UPDATE … status='failed'` inside `retry_or_fail()`. It *is* named in
`docs/ORCHESTRATION.md` pseudocode, so it reads as a leftover from before `retry_or_fail` absorbed the
failure write.
**Action:** delete `mark_failed` (and adjust the ORCHESTRATION.md pseudocode to reference
`retry_or_fail`), or keep it as a deliberate public ledger primitive. Currently dead either way.

### 4. Duplicated default: `EVAL_ENGINE_SAMPLE_TIME_LIMIT`
Read in two modules with the **same hard-coded default `"600"`**:
- `runner.py` → `SAMPLE_TIME_LIMIT`
- `ops.py` → `SAMPLE_TIME_LIMIT_S` (used to derive the sandbox-reaper cutoff)

This is exactly the "a default must not silently diverge between two readers" case that `config.py`
exists to prevent (cf. `STALE_LEADER_SECONDS`, `GLOBAL_MAX_RUNNING`). If one default is ever changed,
the reaper window and the actual sample cap drift apart.
**Action:** promote `SAMPLE_TIME_LIMIT` into `config.py` and have both modules import it.

### 5. `training_analysis.diagnose()` takes an unused `throughput_drop`
The keyword-only `throughput_drop` param is accepted (and passed by `_build_anomaly`) but never read in
the function body — the throughput signal only influences `build_signals()`, not the diagnosis label.
**Action:** either use it in a rule (e.g. a throughput stall corroborating a bad-checkpoint/shard
diagnosis) or drop the parameter. Harmless today; just misleading.

### 6. `runner.batch_load()` shadows its `ids` parameter
```python
def batch_load(run_id, spec, ids=None):
    ...
    rows = control.fetch_unloaded(run_id, ids)   # uses the param
    tuples, ids = [], []                          # then rebinds `ids` to a fresh local
```
Works correctly, but reusing the name for two different things in one function is a readability trap.
**Action (minor):** rename the local to `loaded_ids`.

### 7. Two idioms for splitting `provider/model`
`runner.py` has `_split_model()` (`model.split("/", 1)`) and, separately, `_cost_usd()` uses
`model.partition("/")`. Same operation, two spellings.
**Action (minor):** standardize on `_split_model` (or `partition`) for consistency.

### 8. `db_migrate.apply()` computes `to_apply()` twice
```python
pending = backend.to_apply(migrations)
if pending:
    log.info("applying %d pending …", len(list(pending)))
backend.apply_migrations(backend.to_apply(migrations))   # recomputed
```
Harmless (yoyo's `to_apply` returns a re-evaluable `MigrationList`) but redundant; the second call can
reuse `pending`.
**Action (minor):** `backend.apply_migrations(pending)`.

### 9. `datasets.load_jsonl()` applies `limit` last
It parses **every** line and attaches a per-sample SWE-bench sandbox to **every** sample, then slices
to `limit` at the end — so for a limited run it builds (and writes sandbox spec files for) samples it
then throws away. Negligible for the current small JSONL datasets; would matter at scale.
**Action (minor):** truncate to `limit` before the sandbox attach loop.

---

## Style notes (non-actionable, FYI)
- `runner.py`: `TRANSCRIPT_SAMPLE_RATE = (lambda v: float(v) if v else None)(os.environ.get(...))` —
  an immediately-invoked lambda where a small helper would read more plainly.
- `ops.py` `_comp(logs_url="__auto__")` uses a string sentinel for "auto-derive"; a module-level
  `_AUTO = object()` sentinel would be less collision-prone (cosmetic).

## Checked and fine (no action)
- No genuinely unused imports (the three the AST scan flagged — `builtins` in `api.py`/`cli.py`,
  `k8s_sandbox` in `builtins.py` — are deliberate side-effect/registration imports).
- The builtin scorer/harness factories (`includes_scorer`, `match_scorer`, `choice_scorer`,
  `math_scorer`, `multiple_choice_harness`, …) look "unreferenced" but are resolved by string through
  the plugin registry — not dead.
- `models.py`, `ifeval.py`, `mock_trainer.py`, `storage.py`, `db.py`, `plugins.py`, `config.py`,
  `logs.py`, `view_main.py` are clean.
