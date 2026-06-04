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

control.init() if hasattr(control, "init") else None
analytics.init() if hasattr(analytics, "init") else None

__all__ = ["control", "analytics", "BACKEND"]
