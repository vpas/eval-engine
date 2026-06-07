"""Control plane (Postgres): runs metadata + the ephemeral sample-task ledger, plus the entity
registry and audit log (SCHEMA §1, ORCHESTRATION §4–§10).

The ledger claim is a real ``FOR UPDATE SKIP LOCKED`` over row locks, so concurrent workers claim
disjoint shards with no global write lock — the production scheduling primitive (ORCHESTRATION §6).

Connection via ``EVAL_ENGINE_PG_DSN`` (defaults to the local docker Postgres). One autocommit
connection per thread, so concurrent workers get independent sessions / real row contention.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

import psycopg

from .logs import get_logger

log = get_logger(__name__)

DATA = Path(".data")  # transcripts still land on the object-store stand-in
DATA.mkdir(exist_ok=True)

DSN = os.environ.get(
    "EVAL_ENGINE_PG_DSN",
    "host=localhost port=5433 dbname=evalengine user=evalengine password=evalengine",
)


def _dsn_host() -> str:
    """host=… token from the DSN for logs — never the password (DSN carries credentials)."""
    return next((tok.split("=", 1)[1] for tok in DSN.split() if tok.startswith("host=")), "?")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, eval_id TEXT, eval_version INT, model TEXT, provider TEXT,
  model_id TEXT, harness TEXT, scorers JSONB, status TEXT, total INT, done INT, failed INT,
  accuracy DOUBLE PRECISION, cost_usd DOUBLE PRECISION DEFAULT 0, dataset_hash TEXT, spec_json TEXT,
  created_by TEXT, team TEXT, image_digest TEXT, lane TEXT, max_inflight INT, provider_fingerprint TEXT,
  created_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ);
-- idempotent migrations for tables created before these columns existed
ALTER TABLE runs ADD COLUMN IF NOT EXISTS created_by TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION DEFAULT 0;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS team TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS image_digest TEXT;  -- repro pin: worker code/image (DESIGN §14)
ALTER TABLE runs ADD COLUMN IF NOT EXISTS lane TEXT;          -- interactive | batch (SCHEDULER §2)
ALTER TABLE runs ADD COLUMN IF NOT EXISTS max_inflight INT;   -- per-run concurrency cap (SCHEDULER §3)
ALTER TABLE runs ADD COLUMN IF NOT EXISTS provider_fingerprint TEXT;  -- repro pin: resolved model[@system_fingerprint] (DESIGN §14)
-- Training-monitor provenance (docs/TRAINING_MONITOR.md §2): a checkpoint-eval run is an ordinary run
-- TAGGED with the training run / checkpoint / step + a `sweep` group. NULL for every ad-hoc run.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS training_run_id TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS checkpoint_id TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS step INT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS sweep TEXT;
CREATE INDEX IF NOT EXISTS ix_runs_ckpt ON runs(checkpoint_id);
CREATE INDEX IF NOT EXISTS ix_runs_train ON runs(training_run_id);

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

-- Registered, versioned entities (datasets / evals / models — DESIGN §7, FR1–3). Versions are
-- immutable; re-registering an id mints a new version. One generic table; the shape lives in the body.
CREATE TABLE IF NOT EXISTS entities(
  kind TEXT, id TEXT, version INT, body JSONB, created_by TEXT,
  created_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY(kind, id, version));

-- Append-only audit log (DESIGN §8/§13): who did what, when. Mutations record an entry.
CREATE TABLE IF NOT EXISTS audit_log(
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(),
  actor TEXT, action TEXT, target TEXT, detail JSONB);

-- Liveness heartbeats (ops dashboard): the orchestrator + each worker upsert their row every
-- loop, so the otherwise-invisible singleton orchestrator + KEDA-scaled workers become observable
-- WITHOUT coupling to the Kubernetes API (portable by interface — works on EKS/AKS/local too). The
-- ops snapshot derives liveness from the row's age; `detail` carries per-tick metrics (leader id,
-- claims/loop, admit/finalize counts). One row per (component, instance).
CREATE TABLE IF NOT EXISTS heartbeats(
  component TEXT, instance TEXT, ts TIMESTAMPTZ DEFAULT now(), detail JSONB,
  PRIMARY KEY(component, instance));

-- ===== Training monitor (docs/TRAINING_MONITOR.md) ========================================
-- A monitored (mocked) training run; MUTABLE (status/current_step advance), so a dedicated table
-- rather than the immutable entity registry. `body` holds the full TrainingRunSpec (suite/config/…).
CREATE TABLE IF NOT EXISTS training_runs(
  id TEXT PRIMARY KEY, model TEXT, base TEXT, status TEXT DEFAULT 'watching',
  current_step INT DEFAULT 0, planned_steps INT, source TEXT, owner TEXT, body JSONB,
  created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(), finished_at TIMESTAMPTZ);

-- One discovered checkpoint. `model_ref` is the opaque handle we eval; `train_metrics` is the optional
-- trainer telemetry that powers the §8 cross-check. UNIQUE(run,step) makes discovery idempotent.
CREATE TABLE IF NOT EXISTS checkpoints(
  id TEXT PRIMARY KEY, training_run_id TEXT, step INT, model_ref TEXT, tokens BIGINT,
  wall_time TIMESTAMPTZ, status TEXT DEFAULT 'discovered', train_metrics JSONB,
  discovered_at TIMESTAMPTZ DEFAULT now(), UNIQUE(training_run_id, step));
CREATE INDEX IF NOT EXISTS ix_ckpt_run ON checkpoints(training_run_id, step);

-- Per-(training_run, eval, step) score rollup — the time series the chart + detectors read. Computed
-- at reconcile from the per-checkpoint run's analytics summary (kept in PG so we don't re-query CH).
CREATE TABLE IF NOT EXISTS checkpoint_scores(
  training_run_id TEXT, eval_id TEXT, step INT, run_id TEXT, n INT, passed INT,
  accuracy DOUBLE PRECISION, ci_lo DOUBLE PRECISION, ci_hi DOUBLE PRECISION,
  sample_errors INT, expected DOUBLE PRECISION,
  PRIMARY KEY(training_run_id, eval_id, step));

-- A detected anomaly + its diagnosis (one per (run,eval,step); a sustained dip collapses to one row).
CREATE TABLE IF NOT EXISTS anomalies(
  id TEXT PRIMARY KEY, training_run_id TEXT, eval_id TEXT, step INT, kind TEXT, severity TEXT,
  delta DOUBLE PRECISION, from_step INT, diagnosis TEXT, cause TEXT, signals JSONB,
  categories JSONB, samples JSONB, created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(training_run_id, eval_id, step));

-- MOCK-ONLY: the checkpoint-ref → real-model resolver (§4). In production the gateway resolves a
-- `checkpoint:…` alias natively; here a small table maps it to an OpenRouter/mock model so the rest of
-- the engine treats the checkpoint as "served by trainer infra" without knowing it's faked.
CREATE TABLE IF NOT EXISTS checkpoint_models(
  model_ref TEXT PRIMARY KEY, real_model TEXT, mock_output TEXT, params JSONB);
"""

_local = threading.local()
_init_done = False


def _conn() -> psycopg.Connection:
    con = getattr(_local, "con", None)
    if con is None or con.closed:
        log.debug("opening Postgres connection to host=%s (thread=%s)",
                  _dsn_host(), threading.current_thread().name)
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


def release_leader(key: int) -> None:
    """Graceful handover: explicitly release the advisory lock + close the connection. On pooled PG
    (pgbouncer) merely closing the client connection returns the SERVER connection to the pool with the
    session lock still held — so we must ``pg_advisory_unlock`` first. Called from the orchestrator's
    SIGTERM handler so a rollout hands leadership over in ~1s instead of stalling (bug B1)."""
    global _leader_con
    try:
        if _leader_con is not None and not _leader_con.closed:
            _leader_con.execute("SELECT pg_advisory_unlock(%s)", (key,))
            _leader_con.close()
    except Exception:  # noqa: BLE001
        pass


def reap_stale_leader(key: int, idle_seconds: float = 20.0) -> int:
    """Backstop for an UNgraceful leader death (SIGKILL/OOM/node loss) where SIGTERM never ran: a dead
    orchestrator's pooled connection lingers idle holding the lock, so the standby never gets it. A
    LIVE leader refreshes its lock connection every tick (``leader_alive`` SELECT 1), so it never idles
    this long — making idle>threshold a safe 'crashed' signal. Terminates such a holder; returns # reaped."""
    classid, objid = key >> 32, key & 0xFFFFFFFF
    rows = _conn().execute(
        "SELECT pg_terminate_backend(l.pid) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
        "WHERE l.locktype='advisory' AND l.granted AND l.classid=%s AND l.objid=%s "
        "AND a.state='idle' AND a.state_change < now() - make_interval(secs => %s)",
        (classid, objid, idle_seconds),
    ).fetchall()
    return len(rows)


def init() -> None:
    global _init_done
    if not _init_done:
        _conn().execute(SCHEMA)
        _init_done = True
        log.info("Postgres schema ensured (host=%s)", _dsn_host())


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- runs

def create_run(meta: dict) -> None:
    _conn().execute(
        "INSERT INTO runs(id,eval_id,eval_version,model,provider,model_id,harness,scorers,"
        "status,total,done,failed,dataset_hash,spec_json,created_by,team,image_digest,lane,max_inflight,"
        "training_run_id,checkpoint_id,step,sweep) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'queued',%s,0,0,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (meta["id"], meta["eval_id"], meta["eval_version"], meta["model"], meta["provider"],
         meta["model_id"], meta["harness"], json.dumps(meta["scorers"]), meta["total"],
         meta["dataset_hash"], meta.get("spec_json"), meta.get("created_by"),
         meta.get("team"), meta.get("image_digest"), meta.get("lane"), meta.get("max_inflight"),
         meta.get("training_run_id"), meta.get("checkpoint_id"), meta.get("step"), meta.get("sweep")),
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


def set_fingerprint(run_id: str, fingerprint: str) -> None:
    """Record the provider's resolved-model version fingerprint (repro pin, DESIGN §14). First writer
    wins (``WHERE … IS NULL``) — cheap to call per batch; a run pins the first fingerprint it sees."""
    _conn().execute(
        "UPDATE runs SET provider_fingerprint=%s WHERE id=%s AND provider_fingerprint IS NULL",
        (fingerprint, run_id),
    )


def finalize_run(run_id: str, done: int, failed: int, accuracy: float, cost_usd: float = 0.0,
                 status: str = "completed") -> None:
    _conn().execute(
        "UPDATE runs SET status=%s, done=%s, failed=%s, accuracy=%s, cost_usd=%s, finished_at=now() "
        "WHERE id=%s",
        (status, done, failed, accuracy, cost_usd, run_id),
    )


def live_rollup(run_id: str) -> tuple[int, int, int, float]:
    """One-pass live (done, failed, passed_so_far, cost_so_far) over committed ledger rows — the
    orchestrator's per-tick runs-row rollup (DESIGN §8 "Live metrics")."""
    r = _conn().execute(
        "SELECT count(*) FILTER (WHERE status='done'), count(*) FILTER (WHERE status='failed'), "
        "coalesce(sum(passed) FILTER (WHERE status='done'), 0), "
        "coalesce(sum(cost_usd) FILTER (WHERE status='done'), 0) FROM sample_tasks WHERE run_id=%s",
        (run_id,),
    ).fetchone()
    return int(r[0]), int(r[1]), int(r[2] or 0), float(r[3] or 0.0)


def update_live(run_id: str, done: int, failed: int, accuracy: float, cost_usd: float) -> None:
    """Write the live rollup onto the runs row so clients read live progress/score/cost from one place."""
    _conn().execute(
        "UPDATE runs SET done=%s, failed=%s, accuracy=%s, cost_usd=%s WHERE id=%s",
        (done, failed, accuracy, cost_usd, run_id),
    )


def list_runs():
    # Column order must match api.list_runs() cols. sweep/eval_version/cost_usd surface the
    # checkpoint-sweep badge, eval@version, and cost on the dashboard runs table.
    return _conn().execute(
        "SELECT id, eval_id, eval_version, model, accuracy, total, cost_usd, created_at, "
        "created_by, status, sweep FROM runs ORDER BY created_at DESC"
    ).fetchall()


# Explicit column order for get_run (NOT SELECT * — the table has spec_json/created_by the API doesn't
# map, so positional SELECT * would misalign created_at/finished_at). Keep in sync with api.get_run.
RUN_COLS = ("id, eval_id, eval_version, model, provider, model_id, harness, scorers, status, total, "
            "done, failed, accuracy, cost_usd, dataset_hash, created_by, team, image_digest, lane, "
            "created_at, finished_at, provider_fingerprint")


def get_run(run_id: str):
    return _conn().execute(f"SELECT {RUN_COLS} FROM runs WHERE id=%s", (run_id,)).fetchone()


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
        "  ORDER BY sample_id"
        # Per-run concurrency cap (SCHEDULER §3): headroom = max_inflight − LIVE running (expired
        # leases don't count, so reclaim still works). A run at its cap yields no claims → workers
        # flow to runs with headroom. NULL max_inflight (legacy/uncapped) → effectively unbounded.
        "  LIMIT LEAST(%s, GREATEST(0,"
        "    coalesce((SELECT max_inflight FROM runs WHERE id=%s), 1000000000)"
        "    - (SELECT count(*) FROM sample_tasks WHERE run_id=%s AND status='running'"
        "       AND lease_expires_at > now())))"
        "  FOR UPDATE SKIP LOCKED"
        ") c WHERE t.run_id=c.run_id AND t.sample_id=c.sample_id "
        "RETURNING t.sample_id",
        (worker, lease_seconds, run_id, n, run_id, run_id),
    ).fetchall()
    return [r[0] for r in rows]


def renew_lease(run_id: str, ids: list[str], worker: str, lease_seconds: float = 600.0) -> int:
    """Lease heartbeat: extend the lease on tasks WE still hold and are still executing. A long batch
    (agentic / SWE-bench: image pull + multi-turn agent + test run) can outlive the claim lease, after
    which another worker would reclaim the still-running task and redo it. The worker calls this
    periodically while it executes; if the worker dies, renewal stops and the lease lapses → reclaim
    (crash safety preserved). Guarded by ``claimed_by`` + ``status='running'`` so we never extend a row
    another worker has since reclaimed or that's already committed. Returns # rows renewed."""
    if not ids:
        return 0
    return _conn().execute(
        "UPDATE sample_tasks SET lease_expires_at=now() + make_interval(secs => %s) "
        "WHERE run_id=%s AND sample_id = ANY(%s) AND claimed_by=%s AND status='running'",
        (lease_seconds, run_id, list(ids), worker),
    ).rowcount


def lane_running_counts() -> dict[str, int]:
    """Count of currently-RUNNING runs per lane — input to two-lane admission (SCHEDULER §2)."""
    rows = _conn().execute(
        "SELECT coalesce(lane, 'batch'), count(*) FROM runs WHERE status='running' GROUP BY lane"
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def queued_runs_with_lane() -> list[tuple[str, str]]:
    """Queued runs in FIFO (created_at) order with their lane — the admission candidates."""
    rows = _conn().execute(
        "SELECT id, coalesce(lane, 'batch') FROM runs WHERE status='queued' ORDER BY created_at"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


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


def attempts_for(run_id: str, sample_ids: list[str]) -> dict[str, int]:
    """The current attempt count per still-claimed sample — the ReplacingMergeTree version for the
    analytics insert (read while the row is ``running``, before the ack-before-flip commit)."""
    if not sample_ids:
        return {}
    rows = _conn().execute(
        "SELECT sample_id, attempts FROM sample_tasks WHERE run_id=%s AND sample_id = ANY(%s)",
        (run_id, list(sample_ids)),
    ).fetchall()
    return {sid: int(a or 1) for sid, a in rows}


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


def list_sample_tasks(run_id: str, limit: int = 2000) -> list[dict]:
    """Per-sample ledger rows for a live run — the run page's live sample list. Running samples first
    (with their claiming worker + remaining lease), then queued, then terminal. Empty once the run
    finalizes and the ledger is pruned (state then lives in analytics/object store)."""
    rows = _conn().execute(
        "SELECT sample_id, status, attempts, claimed_by, group_key, error_type, "
        "EXTRACT(EPOCH FROM lease_expires_at - now()) "
        "FROM sample_tasks WHERE run_id=%s "
        "ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, sample_id "
        "LIMIT %s",
        (run_id, limit),
    ).fetchall()
    return [{"sample_id": r[0], "status": r[1], "attempts": r[2], "claimed_by": r[3],
             "group_key": r[4], "error_type": r[5],
             "lease_s": round(float(r[6])) if r[6] is not None else None} for r in rows]


def ledger_size(run_id: str | None = None) -> int:
    if run_id:
        return _conn().execute(
            "SELECT count(*) FROM sample_tasks WHERE run_id=%s", (run_id,)
        ).fetchone()[0]
    return _conn().execute("SELECT count(*) FROM sample_tasks").fetchone()[0]


# --------------------------------------------------------------------------- entity registry (FR1–3)

def register_entity(kind: str, ent_id: str, version: int, body: dict, created_by: str | None = None) -> None:
    """Register an immutable entity version (re-registering the same (kind,id,version) overwrites it —
    convenient in dev; bump the version for a real new revision)."""
    _conn().execute(
        "INSERT INTO entities(kind,id,version,body,created_by) VALUES(%s,%s,%s,%s,%s) "
        "ON CONFLICT(kind,id,version) DO UPDATE SET body=EXCLUDED.body, created_by=EXCLUDED.created_by",
        (kind, ent_id, version, json.dumps(body), created_by),
    )


def list_entities(kind: str) -> list[dict]:
    """Latest version of each id of this kind."""
    rows = _conn().execute(
        "SELECT DISTINCT ON (id) id, version, body, created_by, created_at FROM entities "
        "WHERE kind=%s ORDER BY id, version DESC",
        (kind,),
    ).fetchall()
    return [{"id": r[0], "version": r[1], "body": r[2], "created_by": r[3],
             "created_at": r[4].isoformat() if r[4] else None} for r in rows]


def get_entity(kind: str, ent_id: str, version: int | None = None) -> dict | None:
    """A specific entity version, or the latest when ``version`` is None."""
    if version is None:
        row = _conn().execute(
            "SELECT id, version, body, created_by, created_at FROM entities WHERE kind=%s AND id=%s "
            "ORDER BY version DESC LIMIT 1", (kind, ent_id),
        ).fetchone()
    else:
        row = _conn().execute(
            "SELECT id, version, body, created_by, created_at FROM entities WHERE kind=%s AND id=%s "
            "AND version=%s", (kind, ent_id, version),
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "version": row[1], "body": row[2], "created_by": row[3],
            "created_at": row[4].isoformat() if row[4] else None}


# --------------------------------------------------------------------------- audit log (§8/§13)

def audit(actor: str | None, action: str, target: str, detail: dict | None = None) -> None:
    """Append an audit entry for a mutating action (who did what to which target)."""
    _conn().execute(
        "INSERT INTO audit_log(actor, action, target, detail) VALUES(%s,%s,%s,%s)",
        (actor, action, target, json.dumps(detail) if detail is not None else None),
    )


def list_audit(limit: int = 100) -> list[dict]:
    rows = _conn().execute(
        "SELECT ts, actor, action, target, detail FROM audit_log ORDER BY id DESC LIMIT %s", (limit,)
    ).fetchall()
    return [{"ts": r[0].isoformat() if r[0] else None, "actor": r[1], "action": r[2],
             "target": r[3], "detail": r[4]} for r in rows]


# --------------------------------------------------------------------------- ops / heartbeats

def heartbeat(component: str, instance: str, detail: dict | None = None) -> None:
    """Upsert this process's liveness row (ts=now()). Called every loop by the orchestrator + each
    worker so the ops dashboard can see them without the Kubernetes API (portable liveness)."""
    _conn().execute(
        "INSERT INTO heartbeats(component, instance, ts, detail) VALUES(%s,%s,now(),%s) "
        "ON CONFLICT(component, instance) DO UPDATE SET ts=now(), detail=EXCLUDED.detail",
        (component, instance, json.dumps(detail or {})),
    )


def list_heartbeats() -> list[dict]:
    """Every heartbeat with its age in seconds (the ops snapshot decides live/stale from the age)."""
    rows = _conn().execute(
        "SELECT component, instance, EXTRACT(EPOCH FROM now()-ts), detail FROM heartbeats "
        "ORDER BY component, instance"
    ).fetchall()
    return [{"component": r[0], "instance": r[1], "age_s": float(r[2] or 0.0), "detail": r[3] or {}}
            for r in rows]


def prune_heartbeats(max_age_seconds: float = 3600.0) -> int:
    """Drop heartbeats older than the cutoff (a long-gone worker pod). Returns # removed."""
    rows = _conn().execute(
        "DELETE FROM heartbeats WHERE ts < now() - make_interval(secs => %s) RETURNING instance",
        (max_age_seconds,),
    ).fetchall()
    return len(rows)


def global_ledger_counts() -> dict:
    """Live ledger status counts across ALL runs (queued/running/done/failed/budget_skipped) — the
    cluster-wide queue-depth picture for the ops dashboard."""
    return dict(
        _conn().execute("SELECT status, count(*) FROM sample_tasks GROUP BY status").fetchall()
    )


def run_status_counts() -> dict:
    """Counts of runs by status — the at-a-glance run mix (running/queued/completed/failed/…)."""
    return dict(_conn().execute("SELECT status, count(*) FROM runs GROUP BY status").fetchall())


def pg_connections() -> int:
    """Open backends on this database — a cheap saturation signal for the control plane."""
    row = _conn().execute(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
    ).fetchone()
    return int(row[0]) if row else 0


def active_runs_detail() -> list[dict]:
    """Running/queued runs with their live in-flight + queued ledger counts (one join) — the ops
    'active runs' table, each row drillable to its worker logs by run_id."""
    rows = _conn().execute(
        "SELECT r.id, coalesce(r.lane,'batch'), r.status, coalesce(r.total,0), coalesce(r.done,0), "
        "coalesce(r.failed,0), coalesce(r.cost_usd,0), r.created_at, r.model, "
        "coalesce(t.queued,0), coalesce(t.running,0) "
        "FROM runs r LEFT JOIN ("
        "  SELECT run_id, count(*) FILTER (WHERE status='queued') queued, "
        "         count(*) FILTER (WHERE status='running') running "
        "  FROM sample_tasks GROUP BY run_id) t ON t.run_id = r.id "
        "WHERE r.status IN ('queued','running') ORDER BY r.created_at"
    ).fetchall()
    return [{"id": r[0], "lane": r[1], "status": r[2], "total": r[3], "done": r[4], "failed": r[5],
             "cost_usd": float(r[6] or 0.0), "created_at": r[7].isoformat() if r[7] else None,
             "model": r[8], "queued": r[9], "running": r[10]} for r in rows]


def recent_failures(limit: int = 20) -> list[dict]:
    """Most recent terminal failures (archived once a run finalizes) with the run's model/eval for
    context + drill-in. Newest runs first (the archive has no per-row ts)."""
    rows = _conn().execute(
        "SELECT a.run_id, a.sample_id, a.error_type, a.attempts, r.eval_id, r.model, r.finished_at "
        "FROM failed_task_archive a LEFT JOIN runs r ON r.id = a.run_id "
        "ORDER BY r.finished_at DESC NULLS LAST LIMIT %s",
        (limit,),
    ).fetchall()
    return [{"run_id": r[0], "sample_id": r[1], "error_type": r[2], "attempts": r[3],
             "eval_id": r[4], "model": r[5], "finished_at": r[6].isoformat() if r[6] else None}
            for r in rows]


def archive_and_prune(run_id: str) -> None:
    con = _conn()
    con.execute(
        "INSERT INTO failed_task_archive(run_id,sample_id,error_type,attempts) "
        "SELECT run_id,sample_id,error_type,attempts FROM sample_tasks "
        "WHERE run_id=%s AND status IN ('failed','budget_skipped') ON CONFLICT DO NOTHING",
        (run_id,),
    )
    con.execute("DELETE FROM sample_tasks WHERE run_id=%s", (run_id,))


# ===== Training monitor (docs/TRAINING_MONITOR.md) ==========================================

def create_training_run(spec: dict) -> None:
    """Register a training run to monitor (idempotent on id — re-register updates its spec/status)."""
    _conn().execute(
        "INSERT INTO training_runs(id,model,base,status,current_step,planned_steps,source,owner,body) "
        "VALUES(%s,%s,%s,'watching',0,%s,%s,%s,%s) "
        "ON CONFLICT(id) DO UPDATE SET model=EXCLUDED.model, base=EXCLUDED.base, "
        "planned_steps=EXCLUDED.planned_steps, source=EXCLUDED.source, owner=EXCLUDED.owner, "
        "body=EXCLUDED.body, updated_at=now()",
        (spec["id"], spec.get("model", ""), spec.get("base", ""), spec.get("planned_steps"),
         spec.get("source", ""), spec.get("owner", ""), json.dumps(spec)),
    )


def update_training_run(tr_id: str, *, status: str | None = None, current_step: int | None = None,
                        finished: bool = False) -> None:
    sets, vals = ["updated_at=now()"], []
    if status is not None:
        sets.append("status=%s"); vals.append(status)
    if current_step is not None:
        sets.append("current_step=GREATEST(coalesce(current_step,0), %s)"); vals.append(current_step)
    if finished:
        sets.append("finished_at=now()")
    vals.append(tr_id)
    _conn().execute(f"UPDATE training_runs SET {', '.join(sets)} WHERE id=%s", vals)


def _tr_row(r) -> dict:
    return {"id": r[0], "model": r[1], "base": r[2], "status": r[3], "current_step": r[4],
            "planned_steps": r[5], "source": r[6], "owner": r[7], "body": r[8],
            "created_at": r[9].isoformat() if r[9] else None,
            "finished_at": r[10].isoformat() if r[10] else None}


_TR_COLS = ("id, model, base, status, current_step, planned_steps, source, owner, body, "
            "created_at, finished_at")


def get_training_run(tr_id: str) -> dict | None:
    r = _conn().execute(f"SELECT {_TR_COLS} FROM training_runs WHERE id=%s", (tr_id,)).fetchone()
    return _tr_row(r) if r else None


def list_training_runs() -> list[dict]:
    rows = _conn().execute(f"SELECT {_TR_COLS} FROM training_runs ORDER BY created_at DESC").fetchall()
    return [_tr_row(r) for r in rows]


def active_training_runs() -> list[str]:
    rows = _conn().execute(
        "SELECT id FROM training_runs WHERE status IN ('watching','training') ORDER BY created_at"
    ).fetchall()
    return [r[0] for r in rows]


def insert_checkpoint(ckpt: dict) -> bool:
    """Record a discovered checkpoint. Returns True if newly inserted (idempotent on (run,step))."""
    rows = _conn().execute(
        "INSERT INTO checkpoints(id,training_run_id,step,model_ref,tokens,wall_time,train_metrics) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(training_run_id,step) DO NOTHING RETURNING id",
        (ckpt["id"], ckpt["training_run_id"], ckpt["step"], ckpt["model_ref"], ckpt.get("tokens", 0),
         ckpt.get("wall_time"), json.dumps(ckpt.get("train_metrics", {}))),
    ).fetchall()
    return bool(rows)


def discovered_steps(tr_id: str) -> set[int]:
    rows = _conn().execute(
        "SELECT step FROM checkpoints WHERE training_run_id=%s", (tr_id,)
    ).fetchall()
    return {int(r[0]) for r in rows}


def _ckpt_row(r) -> dict:
    return {"id": r[0], "training_run_id": r[1], "step": r[2], "model_ref": r[3], "tokens": r[4],
            "status": r[5], "train_metrics": r[6] or {},
            "discovered_at": r[7].isoformat() if r[7] else None}


def list_checkpoints(tr_id: str, status: str | None = None) -> list[dict]:
    sql = ("SELECT id,training_run_id,step,model_ref,tokens,status,train_metrics,discovered_at "
           "FROM checkpoints WHERE training_run_id=%s")
    vals: list = [tr_id]
    if status:
        sql += " AND status=%s"; vals.append(status)
    sql += " ORDER BY step"
    return [_ckpt_row(r) for r in _conn().execute(sql, vals).fetchall()]


def set_checkpoint_status(ckpt_id: str, status: str) -> None:
    _conn().execute("UPDATE checkpoints SET status=%s WHERE id=%s", (status, ckpt_id))


def runs_for_checkpoint(ckpt_id: str) -> list[dict]:
    """The per-eval runs launched for a checkpoint + their terminal state (for reconcile)."""
    rows = _conn().execute(
        "SELECT id, eval_id, status, coalesce(failed,0), coalesce(total,0) FROM runs WHERE checkpoint_id=%s",
        (ckpt_id,),
    ).fetchall()
    return [{"run_id": r[0], "eval_id": r[1], "status": r[2], "failed": r[3], "total": r[4]} for r in rows]


def run_for_step(tr_id: str, eval_id: str, step: int) -> str | None:
    """The run_id of a given eval's checkpoint-eval at a step (to diff baseline vs current)."""
    r = _conn().execute(
        "SELECT id FROM runs WHERE training_run_id=%s AND eval_id=%s AND step=%s ORDER BY created_at DESC LIMIT 1",
        (tr_id, eval_id, step),
    ).fetchone()
    return r[0] if r else None


def upsert_checkpoint_score(s: dict) -> None:
    _conn().execute(
        "INSERT INTO checkpoint_scores(training_run_id,eval_id,step,run_id,n,passed,accuracy,ci_lo,ci_hi,"
        "sample_errors,expected) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT(training_run_id,eval_id,step) DO UPDATE SET run_id=EXCLUDED.run_id, n=EXCLUDED.n, "
        "passed=EXCLUDED.passed, accuracy=EXCLUDED.accuracy, ci_lo=EXCLUDED.ci_lo, ci_hi=EXCLUDED.ci_hi, "
        "sample_errors=EXCLUDED.sample_errors, expected=EXCLUDED.expected",
        (s["training_run_id"], s["eval_id"], s["step"], s.get("run_id"), s.get("n"), s.get("passed"),
         s.get("accuracy"), s.get("ci_lo"), s.get("ci_hi"), s.get("sample_errors", 0), s.get("expected")),
    )


def set_checkpoint_expected(tr_id: str, eval_id: str, step: int, expected: float) -> None:
    _conn().execute(
        "UPDATE checkpoint_scores SET expected=%s WHERE training_run_id=%s AND eval_id=%s AND step=%s",
        (expected, tr_id, eval_id, step),
    )


def checkpoint_scores(tr_id: str, eval_id: str | None = None) -> list[dict]:
    sql = ("SELECT eval_id,step,run_id,n,passed,accuracy,ci_lo,ci_hi,sample_errors,expected "
           "FROM checkpoint_scores WHERE training_run_id=%s")
    vals: list = [tr_id]
    if eval_id:
        sql += " AND eval_id=%s"; vals.append(eval_id)
    sql += " ORDER BY eval_id, step"
    rows = _conn().execute(sql, vals).fetchall()
    return [{"eval_id": r[0], "step": r[1], "run_id": r[2], "n": r[3], "passed": r[4],
             "accuracy": r[5], "ci_lo": r[6], "ci_hi": r[7], "sample_errors": r[8], "expected": r[9]}
            for r in rows]


def insert_anomaly(a: dict) -> None:
    _conn().execute(
        "INSERT INTO anomalies(id,training_run_id,eval_id,step,kind,severity,delta,from_step,diagnosis,"
        "cause,signals,categories,samples) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT(training_run_id,eval_id,step) DO UPDATE SET kind=EXCLUDED.kind, "
        "severity=EXCLUDED.severity, delta=EXCLUDED.delta, from_step=EXCLUDED.from_step, "
        "diagnosis=EXCLUDED.diagnosis, cause=EXCLUDED.cause, signals=EXCLUDED.signals, "
        "categories=EXCLUDED.categories, samples=EXCLUDED.samples",
        (a["id"], a["training_run_id"], a["eval_id"], a["step"], a["kind"], a["severity"], a["delta"],
         a.get("from_step"), a.get("diagnosis"), a.get("cause"), json.dumps(a.get("signals", [])),
         json.dumps(a.get("categories", [])), json.dumps(a.get("samples", []))),
    )


def list_anomalies(tr_id: str) -> list[dict]:
    rows = _conn().execute(
        "SELECT id,eval_id,step,kind,severity,delta,from_step,diagnosis,cause,signals,categories,samples "
        "FROM anomalies WHERE training_run_id=%s ORDER BY step DESC", (tr_id,),
    ).fetchall()
    return [{"id": r[0], "eval": r[1], "step": r[2], "kind": r[3], "severity": r[4], "delta": r[5],
             "from": r[6], "diagnosis": r[7], "cause": r[8], "signals": r[9] or [],
             "categories": r[10] or [], "samples": r[11] or []} for r in rows]


# --- mock checkpoint-model resolver (§4) -------------------------------------------------------------

def set_checkpoint_model(model_ref: str, real_model: str, mock_output: str | None = None,
                         params: dict | None = None) -> None:
    _conn().execute(
        "INSERT INTO checkpoint_models(model_ref,real_model,mock_output,params) VALUES(%s,%s,%s,%s) "
        "ON CONFLICT(model_ref) DO UPDATE SET real_model=EXCLUDED.real_model, "
        "mock_output=EXCLUDED.mock_output, params=EXCLUDED.params",
        (model_ref, real_model, mock_output, json.dumps(params or {})),
    )


def get_checkpoint_model(model_ref: str) -> dict | None:
    r = _conn().execute(
        "SELECT real_model, mock_output, params FROM checkpoint_models WHERE model_ref=%s", (model_ref,)
    ).fetchone()
    return {"real_model": r[0], "mock_output": r[1], "params": r[2] or {}} if r else None
