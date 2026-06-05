"""SQLite control plane: runs + **ephemeral sample-task ledger** + failure archive.

Stands in for Postgres (SCHEMA §1). Implements the ledger lifecycle from ORCHESTRATION §4–§10
at single-process scale: expand → claim → commit-result → (caller batch-loads to analytics) →
archive failures + prune. SQLite lacks ``FOR UPDATE SKIP LOCKED``, so the claim is a plain
UPDATE here — the *shape* mirrors production; the concurrency primitive is the one thing that
changes when we move to Postgres + Ray.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import time
import uuid
from pathlib import Path

DATA = Path(".data")
DATA.mkdir(exist_ok=True)
DB = str(DATA / "control.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, eval_id TEXT, eval_version INT, model TEXT, provider TEXT,
  model_id TEXT, harness TEXT, scorers TEXT, status TEXT, total INT, done INT, failed INT,
  accuracy REAL, dataset_hash TEXT, spec_json TEXT, created_by TEXT, created_at TEXT, finished_at TEXT);

CREATE TABLE IF NOT EXISTS sample_tasks(
  run_id TEXT, sample_id TEXT, status TEXT DEFAULT 'queued', attempts INT DEFAULT 0,
  claimed_by TEXT, lease_expires_at REAL, not_before REAL, group_key TEXT,
  passed INT, primary_score REAL, scores TEXT, tokens_in INT, tokens_out INT,
  cost_usd REAL, latency_ms INT, error_type TEXT, transcript_uri TEXT, loaded INT DEFAULT 0,
  PRIMARY KEY(run_id, sample_id));
CREATE INDEX IF NOT EXISTS ix_tasks_claim ON sample_tasks(run_id, status);

CREATE TABLE IF NOT EXISTS failed_task_archive(
  run_id TEXT, sample_id TEXT, error_type TEXT, attempts INT,
  PRIMARY KEY(run_id, sample_id));
"""


def _con() -> sqlite3.Connection:
    # autocommit (isolation_level=None) + WAL + busy_timeout so concurrent workers serialize
    # writes gracefully instead of erroring — the SQLite analogue of row-lock contention.
    con = sqlite3.connect(DB, isolation_level=None, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(SCHEMA)
    return con


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def acquire_leader(key: int) -> bool:
    return True  # single-process local backend: always leader (no contention)


def leader_alive() -> bool:
    return True


def _now() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- runs

def create_run(meta: dict) -> None:
    con = _con()
    con.execute(
        "INSERT INTO runs(id,eval_id,eval_version,model,provider,model_id,harness,scorers,"
        "status,total,done,failed,accuracy,dataset_hash,spec_json,created_by,created_at,finished_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            meta["id"], meta["eval_id"], meta["eval_version"], meta["model"], meta["provider"],
            meta["model_id"], meta["harness"], json.dumps(meta["scorers"]), "queued",
            meta["total"], 0, 0, None, meta["dataset_hash"], meta.get("spec_json"),
            meta.get("created_by"), _now(), None,
        ),
    )
    con.commit()
    con.close()


def get_spec(run_id: str) -> str | None:
    """The persisted RunSpec JSON, so a separate worker/orchestrator process can rehydrate it."""
    con = _con()
    row = con.execute("SELECT spec_json FROM runs WHERE id=?", (run_id,)).fetchone()
    con.close()
    return row[0] if row else None


def active_runs(statuses: tuple[str, ...]) -> list[str]:
    """Run ids currently in any of `statuses` — the work list for workers/orchestrator."""
    con = _con()
    q = "SELECT id FROM runs WHERE status IN (%s) ORDER BY created_at" % ",".join("?" * len(statuses))
    rows = con.execute(q, statuses).fetchall()
    con.close()
    return [r[0] for r in rows]


def run_total(run_id: str) -> int:
    """Authoritative expected sample count (set at create) — the finalize gate vs. expansion races."""
    con = _con()
    row = con.execute("SELECT total FROM runs WHERE id=?", (run_id,)).fetchone()
    con.close()
    return int(row[0]) if row and row[0] is not None else 0


def set_status(run_id: str, status: str) -> None:
    con = _con()
    con.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
    con.commit()
    con.close()


def finalize_run(run_id: str, done: int, failed: int, accuracy: float) -> None:
    con = _con()
    con.execute(
        "UPDATE runs SET status='completed', done=?, failed=?, accuracy=?, finished_at=? WHERE id=?",
        (done, failed, accuracy, _now(), run_id),
    )
    con.commit()
    con.close()


def list_runs():
    con = _con()
    rows = con.execute(
        "SELECT id, eval_id, model, accuracy, total, created_at, created_by FROM runs ORDER BY created_at DESC"
    ).fetchall()
    con.close()
    return rows


def get_run(run_id: str):
    con = _con()
    r = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    con.close()
    return r


# --------------------------------------------------------------------------- ledger

def expand_tasks(run_id: str, items: list[tuple[str, str]]) -> None:
    """Write queued sample-tasks (idempotent — ON CONFLICT/IGNORE, ORCHESTRATION §3)."""
    con = _con()
    con.executemany(
        "INSERT OR IGNORE INTO sample_tasks(run_id,sample_id,group_key) VALUES(?,?,?)",
        [(run_id, sid, gk) for sid, gk in items],
    )
    con.commit()
    con.close()


def claim_batch(run_id: str, worker: str, n: int, lease_seconds: float = 600.0) -> list[str]:
    """Atomically claim up to n tasks that are queued OR running-with-expired-lease.

    Single ``UPDATE ... RETURNING`` under SQLite's write lock → no two workers can claim the
    same row (the SQLite analogue of Postgres ``FOR UPDATE SKIP LOCKED``, ORCHESTRATION §6).
    The ``status='running' AND lease_expires_at < now`` arm reclaims tasks abandoned by a
    crashed worker.
    """
    now = time.time()
    con = _con()
    rows = con.execute(
        "UPDATE sample_tasks SET status='running', attempts=attempts+1, claimed_by=?, "
        "lease_expires_at=? WHERE rowid IN ("
        "  SELECT rowid FROM sample_tasks WHERE run_id=? AND ("
        "    status='queued' OR (status='running' AND lease_expires_at IS NOT NULL "
        "                        AND lease_expires_at < ?))"
        "  ORDER BY sample_id LIMIT ?"
        ") RETURNING sample_id",
        (worker, now + lease_seconds, run_id, now, n),
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def commit_result(run_id: str, sample_id: str, r: dict) -> None:
    """Single-statement commit: result columns + status='done' (exactly-once point, §4/§5)."""
    con = _con()
    con.execute(
        "UPDATE sample_tasks SET status='done', passed=?, primary_score=?, scores=?, "
        "tokens_in=?, tokens_out=?, cost_usd=?, latency_ms=?, error_type=?, transcript_uri=? "
        "WHERE run_id=? AND sample_id=?",
        (
            r["passed"], r["primary_score"], json.dumps(r["scores"]), r["tokens_in"],
            r["tokens_out"], r["cost_usd"], r["latency_ms"], r["error_type"],
            r["transcript_uri"], run_id, sample_id,
        ),
    )
    con.commit()
    con.close()


def mark_failed(run_id: str, sample_id: str, error_type: str) -> None:
    con = _con()
    con.execute(
        "UPDATE sample_tasks SET status='failed', error_type=? WHERE run_id=? AND sample_id=?",
        (error_type, run_id, sample_id),
    )
    con.commit()
    con.close()


def fetch_unloaded(run_id: str, only_ids: list[str] | None = None):
    """done & not-yet-loaded rows → for batch-load into analytics (ORCHESTRATION §4).

    ``only_ids`` scopes the fetch to a specific shard (distributed loaders load just their own)."""
    con = _con()
    sql = ("SELECT sample_id, group_key, passed, primary_score, scores, tokens_in, tokens_out, "
           "cost_usd, latency_ms, error_type, transcript_uri, attempts FROM sample_tasks "
           "WHERE run_id=? AND status='done' AND loaded=0")
    if only_ids is None:
        rows = con.execute(sql, (run_id,)).fetchall()
    else:
        ph = ",".join("?" * len(only_ids))
        rows = con.execute(sql + f" AND sample_id IN ({ph})", (run_id, *only_ids)).fetchall()
    con.close()
    return rows


def mark_loaded(run_id: str, sample_ids: list[str]) -> None:
    if not sample_ids:
        return
    con = _con()
    q = ",".join("?" * len(sample_ids))
    con.execute(
        f"UPDATE sample_tasks SET loaded=1 WHERE run_id=? AND sample_id IN ({q})",
        [run_id, *sample_ids],
    )
    con.commit()
    con.close()


def counts(run_id: str) -> dict:
    con = _con()
    d = dict(
        con.execute(
            "SELECT status, count(*) FROM sample_tasks WHERE run_id=? GROUP BY status", (run_id,)
        ).fetchall()
    )
    con.close()
    return d


def ledger_size(run_id: str | None = None) -> int:
    con = _con()
    if run_id:
        n = con.execute("SELECT count(*) FROM sample_tasks WHERE run_id=?", (run_id,)).fetchone()[0]
    else:
        n = con.execute("SELECT count(*) FROM sample_tasks").fetchone()[0]
    con.close()
    return n


def archive_and_prune(run_id: str) -> None:
    """Archive failures, hard-delete the rest — keeps the ledger ephemeral (ORCHESTRATION §10)."""
    con = _con()
    con.execute(
        "INSERT OR IGNORE INTO failed_task_archive(run_id,sample_id,error_type,attempts) "
        "SELECT run_id,sample_id,error_type,attempts FROM sample_tasks "
        "WHERE run_id=? AND status='failed'",
        (run_id,),
    )
    con.execute("DELETE FROM sample_tasks WHERE run_id=?", (run_id,))
    con.commit()
    con.close()
