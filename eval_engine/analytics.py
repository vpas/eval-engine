"""Analytics store (ClickHouse): the flattened per-sample projection for querying (SCHEMA §2).

ReplacingMergeTree(attempt) keyed on (eval_id, model_id, run_id, sample_id) — a re-executed sample's
higher attempt wins and duplicate re-inserts collapse; monthly partitions; 12-month TTL. Connection
via ``EVAL_ENGINE_CH_*`` env (defaults to the local docker ClickHouse).
"""
from __future__ import annotations

import math
import os

import clickhouse_connect

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
_init_done = False


def _c():
    global _client
    if _client is None:
        _client = clickhouse_connect.get_client(
            host=os.environ.get("EVAL_ENGINE_CH_HOST", "localhost"),
            port=int(os.environ.get("EVAL_ENGINE_CH_PORT", "8123")),
            username=os.environ.get("EVAL_ENGINE_CH_USER", "default"),
            password=os.environ.get("EVAL_ENGINE_CH_PASSWORD", ""),
        )
    return _c_inited()


def _c_inited():
    global _init_done
    if not _init_done:
        _client.command(_ddl())
        _init_done = True
    return _client


def init() -> None:
    _c()


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


def insert(rows: list[tuple]) -> None:
    if not rows:
        return
    _c().insert("sample_results", [list(r) for r in rows], column_names=_COLUMNS,
                settings=_INSERT_SETTINGS)


def _q(sql, params=None):
    return _c().query(sql, parameters=params or {}).result_rows


def _num(x) -> float | int:
    """0 for NULL/NaN. ClickHouse ``avg()`` over an empty set returns NaN (not NULL), and ``NaN or 0``
    stays NaN in Python — which then breaks JSON serialization. A run with no committed rows (e.g. all
    samples failed) must summarize to zeros, not NaN."""
    return 0 if x is None or (isinstance(x, float) and math.isnan(x)) else x


def run_summary(run_id: str):
    # FINAL collapses ReplacingMergeTree dupes for an exact count (ORCHESTRATION §11).
    r = _q(
        "SELECT count(), sum(passed), avg(primary_score), sum(tokens_in+tokens_out), sum(cost_usd) "
        "FROM sample_results FINAL WHERE run_id=%(r)s",
        {"r": run_id},
    )[0]
    return (r[0], _num(r[1]), _num(r[2]), _num(r[3]), _num(r[4]))


def samples(run_id: str):
    return _q(
        "SELECT sample_id, passed, group_key, primary_score, transcript_uri, "
        "(tokens_in + tokens_out) AS tokens, latency_ms, error_type "
        "FROM sample_results FINAL WHERE run_id=%(r)s ORDER BY sample_id",
        {"r": run_id},
    )


def by_category(run_id: str):
    return _q(
        "SELECT group_key, count() n, sum(passed) passed, round(avg(primary_score),3) acc "
        "FROM sample_results FINAL WHERE run_id=%(r)s GROUP BY group_key ORDER BY group_key",
        {"r": run_id},
    )


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
