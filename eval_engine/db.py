"""Backend selector — chooses control/analytics implementations from EVAL_ENGINE_BACKEND.

  EVAL_ENGINE_BACKEND=sqlite (default)  → control (SQLite)   + analytics (DuckDB)
  EVAL_ENGINE_BACKEND=postgres          → control_pg (PG)    + analytics_ch (ClickHouse)

runner / api / cli import ``control`` and ``analytics`` from here, so swapping the whole
storage tier is one env var — the stand-ins and the real backends are interface-compatible.
"""
from __future__ import annotations

import os

BACKEND = os.environ.get("EVAL_ENGINE_BACKEND", "sqlite").lower()

if BACKEND == "postgres":
    from . import analytics_ch as analytics
    from . import control_pg as control
else:
    from . import analytics, control  # noqa: F401


def init() -> None:
    """Ensure schema on both stores. Idempotent. Call at PROCESS STARTUP (API lifespan, CLI,
    worker) — deliberately NOT implicit at import: a container often imports the package before
    its database is reachable, and *importing must never do network I/O that can crash the proc*."""
    control.init() if hasattr(control, "init") else None
    analytics.init() if hasattr(analytics, "init") else None


# Dev ergonomics: best-effort eager init so local sqlite/Postgres "just works" for the prototype
# and the test suite. But tolerate an unreachable DB at import time (the container case) — the
# startup hooks (api.py lifespan, cli.main) call init() again once the DB is guaranteed reachable.
try:
    init()
except Exception:
    pass

__all__ = ["control", "analytics", "BACKEND", "init"]
