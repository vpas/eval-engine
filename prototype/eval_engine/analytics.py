"""DuckDB analytics store — ClickHouse stand-in (SCHEMA §2).

Production-shaped ``sample_results``: typed dims for fast filtering, ``scores`` as a JSON map
(ClickHouse ``Map`` analogue), per-sample ``group_key`` (category) for slice-and-compare,
token/cost columns, and a ``transcript_uri`` pointer (the transcript itself lives in the
object-store stand-in, not here — faithful to "analytics row is small").
"""
from __future__ import annotations

from pathlib import Path

import duckdb

DATA = Path(".data")
DATA.mkdir(exist_ok=True)
DB = str(DATA / "analytics.duckdb")

DDL = """
CREATE TABLE IF NOT EXISTS sample_results(
  run_id VARCHAR, sample_id VARCHAR, eval_id VARCHAR, eval_version INT,
  provider VARCHAR, model_id VARCHAR, harness_type VARCHAR, group_key VARCHAR,
  passed TINYINT, primary_score DOUBLE, scores JSON,
  tokens_in INT, tokens_out INT, cost_usd DOUBLE, latency_ms INT, attempt TINYINT,
  error_type VARCHAR, transcript_uri VARCHAR, review_status VARCHAR, finished_at TIMESTAMP)
"""


def _con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(DB)
    con.execute(DDL)
    return con


def insert(rows: list[tuple]) -> None:
    con = _con()
    con.executemany(
        "INSERT INTO sample_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    con.close()


def run_summary(run_id: str):
    con = _con()
    r = con.execute(
        "SELECT count(*), coalesce(sum(passed),0), coalesce(avg(primary_score),0), "
        "coalesce(sum(tokens_in+tokens_out),0), coalesce(sum(cost_usd),0) "
        "FROM sample_results WHERE run_id=?",
        (run_id,),
    ).fetchone()
    con.close()
    return r  # (n, passed, mean_score, tokens, cost)


def samples(run_id: str):
    con = _con()
    rows = con.execute(
        "SELECT sample_id, passed, group_key, primary_score, transcript_uri "
        "FROM sample_results WHERE run_id=? ORDER BY sample_id",
        (run_id,),
    ).fetchall()
    con.close()
    return rows


def by_category(run_id: str):
    """The canonical slice query: accuracy by category (SCHEMA §2 group_key)."""
    con = _con()
    rows = con.execute(
        "SELECT group_key, count(*) n, sum(passed) passed, round(avg(primary_score),3) acc "
        "FROM sample_results WHERE run_id=? GROUP BY group_key ORDER BY group_key",
        (run_id,),
    ).fetchall()
    con.close()
    return rows


def compare_models_by_category():
    """Cross-run model comparison — what Superset would chart (DESIGN §6.6)."""
    con = _con()
    rows = con.execute(
        "SELECT model_id, group_key, count(*) n, round(avg(primary_score),3) acc "
        "FROM sample_results GROUP BY model_id, group_key ORDER BY model_id, group_key"
    ).fetchall()
    con.close()
    return rows
