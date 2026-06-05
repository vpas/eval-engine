"""Postgres control plane — the REAL backend (SCHEMA §1, ORCHESTRATION §4–§10).

Same function interface as ``control`` (SQLite), so runner/api/cli don't care which is active
(selected in ``db.py``). The one thing that genuinely changes vs SQLite: the claim is a true
``FOR UPDATE SKIP LOCKED`` over row locks — real concurrent claimers, no global write lock.

Connection via ``EVAL_ENGINE_PG_DSN`` (defaults to the local docker Postgres). One autocommit
connection per thread (so concurrent workers get independent sessions / real row contention).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

import psycopg

DATA = Path(".data")  # transcripts still land on the object-store stand-in
DATA.mkdir(exist_ok=True)

DSN = os.environ.get(
    "EVAL_ENGINE_PG_DSN",
    "host=localhost port=5433 dbname=evalengine user=evalengine password=evalengine",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, eval_id TEXT, eval_version INT, model TEXT, provider TEXT,
  model_id TEXT, harness TEXT, scorers JSONB, status TEXT, total INT, done INT, failed INT,
  accuracy DOUBLE PRECISION, dataset_hash TEXT, spec_json TEXT, created_by TEXT,
  created_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ);
-- idempotent migration for tables created before created_by existed
ALTER TABLE runs ADD COLUMN IF NOT EXISTS created_by TEXT;

CREATE TABLE IF NOT EXISTS sample_tasks(
  run_id TEXT, sample_id TEXT, status TEXT DEFAULT 'queued', attempts INT DEFAULT 0,
  claimed_by TEXT, lease_expires_at TIMESTAMPTZ, not_before TIMESTAMPTZ, group_key TEXT,
  passed INT, primary_score DOUBLE PRECISION, scores JSONB, tokens_in INT, tokens_out INT,
  cost_usd DOUBLE PRECISION, latency_ms INT, error_type TEXT, transcript_uri TEXT,
  loaded BOOLEAN DEFAULT false,
  PRIMARY KEY(run_id, sample_id));
CREATE INDEX IF NOT EXISTS ix_tasks_claim ON sample_tasks(run_id, status);
CREATE INDEX IF NOT EXISTS ix_tasks_load ON sample_tasks(run_id) WHERE status='done' AND NOT loaded;

CREATE TABLE IF NOT EXISTS failed_task_archive(
  run_id TEXT, sample_id TEXT, error_type TEXT, attempts INT,
  PRIMARY KEY(run_id, sample_id));
"""

_local = threading.local()
_init_done = False


def _conn() -> psycopg.Connection:
    con = getattr(_local, "con", None)
    if con is None or con.closed:
        con = psycopg.connect(DSN, autocommit=True)
        _local.con = con
    return con


_leader_con: psycopg.Connection | None = None


def acquire_leader(key: int) -> bool:
    """Try to grab a session-scoped advisory lock on a DEDICATED connection (held for the process
    lifetime → released automatically if this process/connection dies). Returns True if we're leader."""
    global _leader_con
    if _leader_con is None or _leader_con.closed:
        _leader_con = psycopg.connect(DSN, autocommit=True)
    return bool(_leader_con.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0])


def leader_alive() -> bool:
    """Liveness of the leader connection holding the advisory lock (False ⟹ we lost leadership)."""
    try:
        _leader_con.execute("SELECT 1")  # type: ignore[union-attr]
        return True
    except Exception:  # noqa: BLE001
        return False


def init() -> None:
    global _init_done
    if not _init_done:
        _conn().execute(SCHEMA)
        _init_done = True


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- runs

def create_run(meta: dict) -> None:
    _conn().execute(
        "INSERT INTO runs(id,eval_id,eval_version,model,provider,model_id,harness,scorers,"
        "status,total,done,failed,dataset_hash,spec_json,created_by) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'queued',%s,0,0,%s,%s,%s)",
        (meta["id"], meta["eval_id"], meta["eval_version"], meta["model"], meta["provider"],
         meta["model_id"], meta["harness"], json.dumps(meta["scorers"]), meta["total"],
         meta["dataset_hash"], meta.get("spec_json"), meta.get("created_by")),
    )


def get_spec(run_id: str) -> str | None:
    """The persisted RunSpec JSON, so a separate worker/orchestrator process can rehydrate it."""
    row = _conn().execute("SELECT spec_json FROM runs WHERE id=%s", (run_id,)).fetchone()
    return row[0] if row else None


def active_runs(statuses: tuple[str, ...]) -> list[str]:
    """Run ids currently in any of `statuses` — the work list for workers/orchestrator."""
    rows = _conn().execute(
        "SELECT id FROM runs WHERE status = ANY(%s) ORDER BY created_at", (list(statuses),)
    ).fetchall()
    return [r[0] for r in rows]


def run_total(run_id: str) -> int:
    """Authoritative expected sample count (set at create) — the finalize gate vs. expansion races."""
    row = _conn().execute("SELECT total FROM runs WHERE id=%s", (run_id,)).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def set_status(run_id: str, status: str) -> None:
    _conn().execute("UPDATE runs SET status=%s WHERE id=%s", (status, run_id))


def finalize_run(run_id: str, done: int, failed: int, accuracy: float,
                 status: str = "completed") -> None:
    _conn().execute(
        "UPDATE runs SET status=%s, done=%s, failed=%s, accuracy=%s, finished_at=now() WHERE id=%s",
        (status, done, failed, accuracy, run_id),
    )


def list_runs():
    return _conn().execute(
        "SELECT id, eval_id, model, accuracy, total, created_at, created_by, status "
        "FROM runs ORDER BY created_at DESC"
    ).fetchall()


def get_run(run_id: str):
    return _conn().execute("SELECT * FROM runs WHERE id=%s", (run_id,)).fetchone()


# --------------------------------------------------------------------------- ledger

def expand_tasks(run_id: str, items: list[tuple[str, str]]) -> None:
    with _conn().cursor() as cur:
        cur.executemany(
            "INSERT INTO sample_tasks(run_id,sample_id,group_key) VALUES(%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            [(run_id, sid, gk) for sid, gk in items],
        )


def claim_batch(run_id: str, worker: str, n: int, lease_seconds: float = 600.0) -> list[str]:
    """The REAL claim: FOR UPDATE SKIP LOCKED over row locks (ORCHESTRATION §6)."""
    rows = _conn().execute(
        "UPDATE sample_tasks t SET status='running', attempts=attempts+1, claimed_by=%s, "
        "lease_expires_at=now() + make_interval(secs => %s) "
        "FROM ("
        "  SELECT run_id, sample_id FROM sample_tasks"
        "  WHERE run_id=%s AND ("
        "        (status='queued' AND (not_before IS NULL OR not_before <= now())) OR"
        "        (status='running' AND lease_expires_at < now()))"
        "  ORDER BY sample_id LIMIT %s"
        "  FOR UPDATE SKIP LOCKED"
        ") c WHERE t.run_id=c.run_id AND t.sample_id=c.sample_id "
        "RETURNING t.sample_id",
        (worker, lease_seconds, run_id, n),
    ).fetchall()
    return [r[0] for r in rows]


def commit_result(run_id: str, sample_id: str, r: dict) -> None:
    _conn().execute(
        "UPDATE sample_tasks SET status='done', passed=%s, primary_score=%s, scores=%s, "
        "tokens_in=%s, tokens_out=%s, cost_usd=%s, latency_ms=%s, error_type=%s, "
        "transcript_uri=%s WHERE run_id=%s AND sample_id=%s",
        (r["passed"], r["primary_score"], json.dumps(r["scores"]), r["tokens_in"],
         r["tokens_out"], r["cost_usd"], r["latency_ms"], r["error_type"],
         r["transcript_uri"], run_id, sample_id),
    )


def mark_failed(run_id: str, sample_id: str, error_type: str) -> None:
    _conn().execute(
        "UPDATE sample_tasks SET status='failed', error_type=%s WHERE run_id=%s AND sample_id=%s",
        (error_type, run_id, sample_id),
    )


def retry_or_fail(run_id: str, sample_id: str, error_type: str, max_attempts: int = 3,
                  base_seconds: float = 2.0, cap_seconds: float = 60.0) -> str:
    """A transient sample failure: re-queue with exponential ``not_before`` backoff if we're under
    the attempt cap (the claim already incremented ``attempts``); otherwise terminal ``failed``
    (ORCHESTRATION §7, FR5). Re-queuing clears the lease so the row is immediately *eligible* but
    not claimable until ``not_before`` — so a poison sample backs off instead of head-of-line
    blocking. Returns ``'retry'`` or ``'failed'``."""
    row = _conn().execute(
        "SELECT attempts FROM sample_tasks WHERE run_id=%s AND sample_id=%s", (run_id, sample_id)
    ).fetchone()
    attempts = int(row[0]) if row and row[0] is not None else max_attempts
    if attempts >= max_attempts:
        _conn().execute(
            "UPDATE sample_tasks SET status='failed', error_type=%s, claimed_by=NULL, "
            "lease_expires_at=NULL WHERE run_id=%s AND sample_id=%s",
            (error_type, run_id, sample_id),
        )
        return "failed"
    delay = min(base_seconds * (2 ** max(0, attempts - 1)), cap_seconds)
    _conn().execute(
        "UPDATE sample_tasks SET status='queued', error_type=%s, claimed_by=NULL, "
        "lease_expires_at=NULL, not_before=now() + make_interval(secs => %s) "
        "WHERE run_id=%s AND sample_id=%s",
        (error_type, delay, run_id, sample_id),
    )
    return "retry"


def run_cost(run_id: str) -> float:
    """Committed cost-so-far (sum over ``done`` ledger rows) — the live budget gauge during a run."""
    row = _conn().execute(
        "SELECT coalesce(sum(cost_usd), 0) FROM sample_tasks WHERE run_id=%s AND status='done'",
        (run_id,),
    ).fetchone()
    return float(row[0] or 0.0)


def budget_stop(run_id: str) -> int:
    """Budget reached: convert still-``queued`` tasks to the DISTINCT terminal ``budget_skipped``
    (error_type ``budget_exceeded``) — NOT ``failed``, so it neither inflates ``failed_samples`` nor
    burns retries (DESIGN §8). In-flight ``running`` tasks finish naturally. Returns # skipped."""
    rows = _conn().execute(
        "UPDATE sample_tasks SET status='budget_skipped', error_type='budget_exceeded' "
        "WHERE run_id=%s AND status='queued' RETURNING sample_id",
        (run_id,),
    ).fetchall()
    return len(rows)


def fetch_unloaded(run_id: str, only_ids: list[str] | None = None):
    cols = ("SELECT sample_id, group_key, passed, primary_score, scores, tokens_in, tokens_out, "
            "cost_usd, latency_ms, error_type, transcript_uri, attempts FROM sample_tasks "
            "WHERE run_id=%s AND status='done' AND NOT loaded")
    if only_ids is None:
        return _conn().execute(cols, (run_id,)).fetchall()
    return _conn().execute(cols + " AND sample_id = ANY(%s)", (run_id, list(only_ids))).fetchall()


def mark_loaded(run_id: str, sample_ids: list[str]) -> None:
    if not sample_ids:
        return
    _conn().execute(
        "UPDATE sample_tasks SET loaded=true WHERE run_id=%s AND sample_id = ANY(%s)",
        (run_id, list(sample_ids)),
    )


def counts(run_id: str) -> dict:
    return dict(
        _conn().execute(
            "SELECT status, count(*) FROM sample_tasks WHERE run_id=%s GROUP BY status", (run_id,)
        ).fetchall()
    )


def ledger_size(run_id: str | None = None) -> int:
    if run_id:
        return _conn().execute(
            "SELECT count(*) FROM sample_tasks WHERE run_id=%s", (run_id,)
        ).fetchone()[0]
    return _conn().execute("SELECT count(*) FROM sample_tasks").fetchone()[0]


def archive_and_prune(run_id: str) -> None:
    con = _conn()
    con.execute(
        "INSERT INTO failed_task_archive(run_id,sample_id,error_type,attempts) "
        "SELECT run_id,sample_id,error_type,attempts FROM sample_tasks "
        "WHERE run_id=%s AND status IN ('failed','budget_skipped') ON CONFLICT DO NOTHING",
        (run_id,),
    )
    con.execute("DELETE FROM sample_tasks WHERE run_id=%s", (run_id,))
