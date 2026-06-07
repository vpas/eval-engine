"""Unit: the new pure-logic surfaces added when adopting psycopg_pool / yoyo / json logging —
the DSN→URL conversion for yoyo and the structured-log format toggle. No backends touched."""
import json
import logging

from eval_engine import logs
from eval_engine.db_migrate import _yoyo_url


# --------------------------------------------------------------------------- yoyo DSN → URL

def test_yoyo_url_from_libpq_keyword_dsn():
    url = _yoyo_url("host=db.local port=5433 dbname=evalengine user=ev password=secret")
    assert url == "postgresql://ev:secret@db.local:5433/evalengine"


def test_yoyo_url_from_uri_dsn_roundtrips():
    # the managed/in-cluster secret is already a URI — conninfo_to_dict parses it, we re-emit the same.
    url = _yoyo_url("postgresql://ev:secret@db.local:5432/evalengine")
    assert url == "postgresql://ev:secret@db.local:5432/evalengine"


def test_yoyo_url_percent_quotes_password():
    # a password with URL-significant chars (@ / :) must be quoted so the URL parses back correctly.
    url = _yoyo_url("host=h port=5432 dbname=d user=u password=p@ss/w:rd")
    assert url == "postgresql://u:p%40ss%2Fw%3Ard@h:5432/d"


# --------------------------------------------------------------------------- structured logging toggle

def test_json_disabled_by_default(monkeypatch):
    monkeypatch.delenv("EVAL_ENGINE_LOG_JSON", raising=False)
    monkeypatch.delenv("EVAL_ENGINE_GCP_PROJECT", raising=False)
    assert logs._json_enabled() is False
    assert not hasattr(logs._formatter(), "add_fields")  # plain logging.Formatter, not the JSON one


def test_json_auto_on_in_cluster(monkeypatch):
    monkeypatch.delenv("EVAL_ENGINE_LOG_JSON", raising=False)
    monkeypatch.setenv("EVAL_ENGINE_GCP_PROJECT", "some-project")  # cluster ⇒ JSON
    assert logs._json_enabled() is True


def test_json_formatter_emits_gcp_severity(monkeypatch):
    monkeypatch.setenv("EVAL_ENGINE_LOG_JSON", "1")
    fmt = logs._formatter()
    rec = logging.LogRecord("eval_engine.worker", logging.WARNING, __file__, 1,
                            "claimed run_id=%s", ("abc123",), None)
    payload = json.loads(fmt.format(rec))
    assert payload["severity"] == "WARNING"          # GCP Cloud Logging reads this field
    assert payload["message"] == "claimed run_id=abc123"
    assert payload["logger"] == "eval_engine.worker"
