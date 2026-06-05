"""Storage tier: the control plane (``control`` = Postgres) and the analytics store
(``analytics`` = ClickHouse).

runner / api / cli / worker / orchestrator import ``control`` and ``analytics`` from here so the
storage tier is referenced through one place. Connection config is via env — ``EVAL_ENGINE_PG_DSN``
and ``EVAL_ENGINE_CH_*`` (see the respective modules), defaulting to the local docker stack so
``infra/up.sh`` + a CLI run "just works" in dev.
"""
from __future__ import annotations

from . import analytics, control  # noqa: F401


def init() -> None:
    """Ensure schema on both stores. Idempotent. Call at PROCESS STARTUP (API lifespan, CLI, worker,
    orchestrator) — deliberately NOT at import: a container often imports the package before its
    database is reachable, and importing must never do network I/O that can crash the process."""
    control.init()
    analytics.init()


__all__ = ["control", "analytics", "init"]
