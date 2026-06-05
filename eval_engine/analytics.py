"""Analytics store (ClickHouse): the flattened per-sample projection for querying (SCHEMA §2).

ReplacingMergeTree(attempt) keyed on (eval_id, model_id, run_id, sample_id) — a re-executed sample's
higher attempt wins and duplicate re-inserts collapse; monthly partitions; 12-month TTL. Connection
via ``EVAL_ENGINE_CH_*`` env (defaults to the local docker ClickHouse).
"""
from __future__ import annotations

import os

import clickhouse_connect

DDL = """
CREATE TABLE IF NOT EXISTS sample_results(
  run_id String, sample_id String, eval_id String, eval_version UInt32,
  provider LowCardinality(String), model_id LowCardinality(String),
  harness_type LowCardinality(String), group_key LowCardinality(String),
  passed UInt8, primary_score Float64, scores String,
  tokens_in UInt32, tokens_out UInt32, cost_usd Float64, latency_ms UInt32, attempt UInt8,
  error_type LowCardinality(String), transcript_uri String,
  review_status LowCardinality(String), finished_at DateTime
)
ENGINE = ReplacingMergeTree(attempt)
PARTITION BY toYYYYMM(finished_at)
ORDER BY (eval_id, model_id, run_id, sample_id)
TTL finished_at + INTERVAL 12 MONTH
"""

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
        _client.command(DDL)
        _init_done = True
    return _client


def init() -> None:
    _c()


_COLUMNS = [
    "run_id", "sample_id", "eval_id", "eval_version", "provider", "model_id", "harness_type",
    "group_key", "passed", "primary_score", "scores", "tokens_in", "tokens_out", "cost_usd",
    "latency_ms", "attempt", "error_type", "transcript_uri", "review_status", "finished_at",
]


def insert(rows: list[tuple]) -> None:
    if not rows:
        return
    _c().insert("sample_results", [list(r) for r in rows], column_names=_COLUMNS)


def _q(sql, params=None):
    return _c().query(sql, parameters=params or {}).result_rows


def run_summary(run_id: str):
    # FINAL collapses ReplacingMergeTree dupes for an exact count (ORCHESTRATION §11).
    r = _q(
        "SELECT count(), sum(passed), avg(primary_score), sum(tokens_in+tokens_out), sum(cost_usd) "
        "FROM sample_results FINAL WHERE run_id=%(r)s",
        {"r": run_id},
    )[0]
    return (r[0], r[1] or 0, r[2] or 0, r[3] or 0, r[4] or 0)


def samples(run_id: str):
    return _q(
        "SELECT sample_id, passed, group_key, primary_score, transcript_uri "
        "FROM sample_results FINAL WHERE run_id=%(r)s ORDER BY sample_id",
        {"r": run_id},
    )


def by_category(run_id: str):
    return _q(
        "SELECT group_key, count() n, sum(passed) passed, round(avg(primary_score),3) acc "
        "FROM sample_results FINAL WHERE run_id=%(r)s GROUP BY group_key ORDER BY group_key",
        {"r": run_id},
    )


def compare_models_by_category():
    return _q(
        "SELECT model_id, group_key, count() n, round(avg(primary_score),3) acc "
        "FROM sample_results FINAL GROUP BY model_id, group_key ORDER BY model_id, group_key"
    )
