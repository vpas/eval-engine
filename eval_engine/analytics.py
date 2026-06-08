"""Analytics store (ClickHouse): the flattened per-sample projection for querying (SCHEMA §2).

ReplacingMergeTree(attempt) keyed on (eval_id, model_id, run_id, sample_id) — a re-executed sample's
higher attempt wins and duplicate re-inserts collapse; monthly partitions; 12-month TTL. Connection
via ``EVAL_ENGINE_CH_*`` env (defaults to the local docker ClickHouse).
"""
from __future__ import annotations

import json
import math
import os
import time
from typing import Callable, NamedTuple, TypeVar

import clickhouse_connect
from clickhouse_connect.driver.exceptions import InterfaceError, OperationalError

from .logs import get_logger

log = get_logger(__name__)

# HA (#16): when EVAL_ENGINE_CH_CLUSTER is set the table is created ON CLUSTER with the *Replicated*
# engine, so every ClickHouse replica holds a copy (Keeper-coordinated) and a replica loss doesn't lose
# results. Unset (dev/CI single node) ⇒ the plain ReplacingMergeTree — identical DDL to before, so
# nothing changes locally. The `{shard}`/`{replica}` are ClickHouse macros (per-pod config), not Python.
CH_CLUSTER = os.environ.get("EVAL_ENGINE_CH_CLUSTER")

_SCHEMA_COLS = """
  run_id String, sample_id String, eval_id String, eval_version UInt32,
  provider LowCardinality(String), model_id LowCardinality(String),
  harness_type LowCardinality(String), group_key LowCardinality(String),
  passed UInt8, primary_score Float64, scores String,
  tokens_in UInt32, tokens_out UInt32, cost_usd Float64, latency_ms UInt32, attempt UInt8,
  error_type LowCardinality(String), transcript_uri String,
  review_status LowCardinality(String), finished_at DateTime"""


def _ddl() -> str:
    on_cluster = f" ON CLUSTER {CH_CLUSTER}" if CH_CLUSTER else ""
    engine = ("ReplicatedReplacingMergeTree('/clickhouse/tables/{shard}/sample_results', '{replica}', attempt)"
              if CH_CLUSTER else "ReplacingMergeTree(attempt)")
    return (f"CREATE TABLE IF NOT EXISTS sample_results{on_cluster}({_SCHEMA_COLS}\n)\n"
            f"ENGINE = {engine}\n"
            "PARTITION BY toYYYYMM(finished_at)\n"
            "ORDER BY (eval_id, model_id, run_id, sample_id)\n"
            "TTL finished_at + INTERVAL 12 MONTH")

_client = None
# Process-global, NOT per-client: the schema is server-side (CREATE … IF NOT EXISTS), so it only needs
# ensuring once per process. (Same rationale + shape as control.py's _conn/_ensure_schema.)
_init_done = False


def _c():
    """The ClickHouse client, connecting + ensuring the schema once per process on first use."""
    global _client, _init_done
    if _client is None:
        host = os.environ.get("EVAL_ENGINE_CH_HOST", "localhost")
        log.debug("opening ClickHouse client to host=%s (cluster=%s)", host, CH_CLUSTER or "-")
        _client = clickhouse_connect.get_client(
            host=host,
            port=int(os.environ.get("EVAL_ENGINE_CH_PORT", "8123")),
            username=os.environ.get("EVAL_ENGINE_CH_USER", "default"),
            password=os.environ.get("EVAL_ENGINE_CH_PASSWORD", ""),
        )
    if not _init_done:
        _client.command(_ddl())
        _init_done = True
        log.info("ClickHouse schema ensured (host=%s, cluster=%s)",
                 os.environ.get("EVAL_ENGINE_CH_HOST", "localhost"), CH_CLUSTER or "-")
    return _client


def init() -> None:
    _c()


# Connection resilience (see docs/RESILIENCE.md; mirrors control._run). The module-global client is
# never re-created on failure on its own, so a transient ClickHouse unavailability (rollout, node loss,
# brief network blip) would otherwise crash the worker mid-commit — and because the ack-before-flip
# commit inserts to ClickHouse BEFORE flipping the ledger to 'done', that stalls execution, not just
# reads. So every insert/query runs through `_run`: on a connection error it drops the cached client
# (schema is server-side, so a rebuilt client skips DDL) and retries with bounded backoff.
_RETRYABLE = (OperationalError, InterfaceError)
_RETRY_TRIES = 3
_RETRY_BASE_S = 0.2
_RETRY_CAP_S = 2.0
_T = TypeVar("_T")


def _run(op: Callable[[clickhouse_connect.driver.Client], _T]) -> _T:
    """Run ``op(client)`` with reconnect-on-broken-connection + bounded-backoff retry. A retried insert
    is safe: ReplacingMergeTree(attempt) collapses an identical re-insert (same key + attempt) on merge
    / FINAL, so a lost-ack double-insert is deduped — the same property the ack-before-flip commit relies
    on for re-claimed samples."""
    global _client
    last: Exception | None = None
    for i in range(_RETRY_TRIES):
        try:
            return op(_c())
        except _RETRYABLE as e:
            last = e
            _client = None  # drop the cached client; _c() rebuilds it (schema already ensured server-side)
            if i == _RETRY_TRIES - 1:
                break
            log.warning("ClickHouse connection lost (%s) — reconnecting, retry %d/%d",
                        e.__class__.__name__, i + 1, _RETRY_TRIES - 1)
            time.sleep(min(_RETRY_BASE_S * (2 ** i), _RETRY_CAP_S))
    raise last  # type: ignore[misc]


_COLUMNS = [
    "run_id", "sample_id", "eval_id", "eval_version", "provider", "model_id", "harness_type",
    "group_key", "passed", "primary_score", "scores", "tokens_in", "tokens_out", "cost_usd",
    "latency_ms", "attempt", "error_type", "transcript_uri", "review_status", "finished_at",
]


# Async insert with a DURABLE ack (DESIGN §8, HA #16): the server buffers concurrent small inserts and
# flushes them as one batch (far better under many workers than a write part per insert), while
# wait_for_async_insert=1 blocks until that batch is committed to the table — so the call still returns
# only once the data is durable. This preserves the ack-before-flip invariant (the ledger flips to
# 'done' only after a durable analytics write); it just changes how the server lands the bytes.
_INSERT_SETTINGS = {"async_insert": 1, "wait_for_async_insert": 1}


def make_row(*, run_id, sample_id, eval_id, eval_version, provider, model_id, harness_type,
             group_key, passed, primary_score, scores, tokens_in, tokens_out, cost_usd,
             latency_ms, attempt, error_type, transcript_uri, finished_at,
             review_status="none") -> tuple:
    """Build one ``sample_results`` row in canonical ``_COLUMNS`` order. Both the commit path and the
    batch-loader insert the same projection from different sources (a result dict vs. a ledger row);
    this is the single place that knows the field→column layout, so a schema change touches one
    builder, not two hand-aligned 20-tuples. Ordering by ``_COLUMNS`` (not literal position) means
    adding a column can't silently shift a value. ``scores`` accepts a dict or an already-serialized
    string (PG JSONB comes back as a dict)."""
    vals = {
        "run_id": run_id, "sample_id": sample_id, "eval_id": eval_id, "eval_version": eval_version,
        "provider": provider, "model_id": model_id, "harness_type": harness_type,
        "group_key": group_key or "", "passed": passed, "primary_score": primary_score,
        "scores": scores if isinstance(scores, str) else json.dumps(scores),
        "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost_usd,
        "latency_ms": latency_ms, "attempt": attempt, "error_type": error_type or "",
        "transcript_uri": transcript_uri or "", "review_status": review_status,
        "finished_at": finished_at,
    }
    return tuple(vals[c] for c in _COLUMNS)


def insert(rows: list[tuple]) -> None:
    if not rows:
        return
    _run(lambda c: c.insert("sample_results", [list(r) for r in rows], column_names=_COLUMNS,
                            settings=_INSERT_SETTINGS))
    log.debug("inserted %d sample row(s) into ClickHouse (run_id=%s)", len(rows), rows[0][0])


def _q(sql, params=None):
    return _run(lambda c: c.query(sql, parameters=params or {}).result_rows)


def _num(x) -> float | int:
    """0 for NULL/NaN. ClickHouse ``avg()`` over an empty set returns NaN (not NULL), and ``NaN or 0``
    stays NaN in Python — which then breaks JSON serialization. A run with no committed rows (e.g. all
    samples failed) must summarize to zeros, not NaN."""
    return 0 if x is None or (isinstance(x, float) and math.isnan(x)) else x


# Named result rows so callers read fields by name, not by position — adding a SELECT column can't
# silently shift a downstream unpack (the drift that left the CLI unpacking the wrong arity). These
# stay tuples, so existing positional unpacking / indexing keeps working unchanged.

class RunSummary(NamedTuple):
    samples: int
    passed: float
    mean_score: float
    tokens: float
    cost: float


class SampleRow(NamedTuple):
    sample_id: str
    passed: int
    group_key: str
    primary_score: float
    transcript_uri: str
    tokens: int
    latency_ms: int
    error_type: str


class CategoryRow(NamedTuple):
    group_key: str
    n: int
    passed: int
    accuracy: float


def run_summary(run_id: str) -> RunSummary:
    # FINAL collapses ReplacingMergeTree dupes for an exact count (ORCHESTRATION §11).
    r = _q(
        "SELECT count(), sum(passed), avg(primary_score), sum(tokens_in+tokens_out), sum(cost_usd) "
        "FROM sample_results FINAL WHERE run_id=%(r)s",
        {"r": run_id},
    )[0]
    return RunSummary(r[0], _num(r[1]), _num(r[2]), _num(r[3]), _num(r[4]))


def samples(run_id: str) -> list[SampleRow]:
    return [SampleRow(*r) for r in _q(
        "SELECT sample_id, passed, group_key, primary_score, transcript_uri, "
        "(tokens_in + tokens_out) AS tokens, latency_ms, error_type "
        "FROM sample_results FINAL WHERE run_id=%(r)s ORDER BY sample_id",
        {"r": run_id},
    )]


def by_category(run_id: str) -> list[CategoryRow]:
    return [CategoryRow(*r) for r in _q(
        "SELECT group_key, count() n, sum(passed) passed, round(avg(primary_score),3) acc "
        "FROM sample_results FINAL WHERE run_id=%(r)s GROUP BY group_key ORDER BY group_key",
        {"r": run_id},
    )]


def health() -> dict:
    """Liveness + key signals for the ops dashboard: an approximate row count (no FINAL — this is a
    gauge, not a metric) and, when running replicated (HA #16), the replica quorum + replication lag
    from ``system.replicas`` so a lost/lagging ClickHouse replica surfaces as degraded."""
    info: dict = {"rows": 0, "replicas": None}
    r = _q("SELECT count() FROM sample_results")
    info["rows"] = int(r[0][0]) if r and r[0][0] is not None else 0
    if CH_CLUSTER:
        rr = _q(
            "SELECT min(total_replicas), min(active_replicas), max(queue_size), max(absolute_delay) "
            "FROM system.replicas WHERE table='sample_results'"
        )
        if rr and rr[0][0] is not None:
            info["replicas"] = {"total": int(rr[0][0]), "active": int(rr[0][1]),
                                "queue": int(rr[0][2] or 0), "delay_s": int(rr[0][3] or 0)}
    return info


def compare_models_by_category():
    return _q(
        "SELECT model_id, group_key, count() n, round(avg(primary_score),3) acc "
        "FROM sample_results FINAL GROUP BY model_id, group_key ORDER BY model_id, group_key"
    )
