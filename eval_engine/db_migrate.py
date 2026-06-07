"""Postgres schema migrations via yoyo-migrations (replaces the hand-rolled CREATE + idempotent-ALTER
string that used to live in ``control.py``).

``control.init()`` calls :func:`apply` at process startup. yoyo keeps its own ledger table
(``_yoyo_migration``) so each ``.sql`` file in ``migrations/`` runs exactly once, and takes a Postgres
advisory lock while applying so concurrent pods racing startup don't double-apply. Adding a schema
change is now "drop a new numbered ``NNNN_*.sql`` file in ``eval_engine/migrations/``", not "append
another ``ALTER TABLE … IF NOT EXISTS`` to a Python string".
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from .logs import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _yoyo_url(dsn: str) -> str:
    """yoyo's Postgres backend speaks a ``postgresql://user:pass@host:port/db`` URL. Our DSN may be in
    either the libpq keyword form (local default) or the URI form (the managed/in-cluster secret);
    ``conninfo_to_dict`` parses both, and we re-emit the URL yoyo wants (password percent-quoted)."""
    from psycopg.conninfo import conninfo_to_dict

    d = conninfo_to_dict(dsn)
    user, pw = d.get("user", ""), d.get("password", "")
    host, port, db = d.get("host", "localhost"), d.get("port", "5432"), d.get("dbname", "")
    auth = user + (f":{quote(str(pw), safe='')}" if pw else "")
    auth = f"{auth}@" if auth else ""
    return f"postgresql://{auth}{host}:{port}/{db}"


def apply(dsn: str) -> None:
    """Apply every pending migration in ``migrations/`` to the database at ``dsn`` (idempotent)."""
    from yoyo import get_backend, read_migrations

    backend = get_backend(_yoyo_url(dsn))
    migrations = read_migrations(str(MIGRATIONS_DIR))
    with backend.lock():
        pending = backend.to_apply(migrations)
        if pending:
            log.info("applying %d pending Postgres migration(s)", len(list(pending)))
        backend.apply_migrations(backend.to_apply(migrations))
