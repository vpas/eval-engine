"""Orchestrator two-lane admission (`_admit`) against the real ledger (SCHEDULER §2).

The rule: interactive runs may use any of GLOBAL_MAX_RUNNING slots; batch runs are capped below a
reserved interactive slice *whenever there is interactive demand* (queued or running), and may
borrow that reserve only when there is none. Caps are shrunk via monkeypatch so a handful of runs
exercises the boundaries. ``clean_db`` (autouse in this dir) gives each test a clean ledger.
"""
from __future__ import annotations

import pytest

from eval_engine import db, orchestrator


def _status(run_id: str) -> str:
    return db.control.get_run(run_id)[8]  # RUN_COLS: …status(8)…


def _order(run_ids: list[str]) -> None:
    """Pin created_at to the list order so FIFO admission is deterministic (avoids same-instant ties)."""
    for k, rid in enumerate(run_ids):
        db.control._conn().execute(
            "UPDATE runs SET created_at = now() + make_interval(secs => %s) WHERE id=%s", (k, rid)
        )


def test_admits_up_to_global_cap(make_run, monkeypatch):
    monkeypatch.setattr(orchestrator, "GLOBAL_MAX_RUNNING", 3)
    monkeypatch.setattr(orchestrator, "INTERACTIVE_RESERVE", 1)
    runs = [make_run(n=1, lane="batch") for _ in range(5)]

    orchestrator._admit()

    statuses = [_status(r) for r in runs]
    assert statuses.count("running") == 3 and statuses.count("queued") == 2, statuses


def test_batch_borrows_reserve_when_no_interactive_demand(make_run, monkeypatch):
    # No interactive run anywhere → batch_ceiling == GLOBAL (the reserve is lent out).
    monkeypatch.setattr(orchestrator, "GLOBAL_MAX_RUNNING", 3)
    monkeypatch.setattr(orchestrator, "INTERACTIVE_RESERVE", 1)
    runs = [make_run(n=1, lane="batch") for _ in range(4)]

    orchestrator._admit()

    assert sum(_status(r) == "running" for r in runs) == 3  # reached GLOBAL, not GLOBAL-RESERVE


def test_interactive_reserve_holds_back_batch(make_run, monkeypatch):
    # Interactive demand present → batch capped at GLOBAL-RESERVE, leaving room for the interactive run.
    monkeypatch.setattr(orchestrator, "GLOBAL_MAX_RUNNING", 3)
    monkeypatch.setattr(orchestrator, "INTERACTIVE_RESERVE", 1)
    batch = [make_run(n=1, lane="batch") for _ in range(3)]
    interactive = make_run(n=1, lane="interactive")
    _order(batch + [interactive])  # batch first in FIFO, interactive last

    orchestrator._admit()

    assert _status(interactive) == "running", "interactive run must always be admitted"
    # batch is held at the ceiling (GLOBAL 3 - RESERVE 1 = 2), so one batch stays queued
    assert sum(_status(b) == "running" for b in batch) == 2
    assert sum(_status(b) == "queued" for b in batch) == 1


def test_running_interactive_also_counts_as_demand(make_run, monkeypatch):
    # Demand can come from an already-RUNNING interactive run, not just a queued one.
    monkeypatch.setattr(orchestrator, "GLOBAL_MAX_RUNNING", 3)
    monkeypatch.setattr(orchestrator, "INTERACTIVE_RESERVE", 1)
    running_inter = make_run(n=1, lane="interactive")
    db.control.set_status(running_inter, "running")  # occupies 1 slot, signals interactive demand
    batch = [make_run(n=1, lane="batch") for _ in range(3)]

    orchestrator._admit()

    # ceiling = GLOBAL-RESERVE = 2; one slot already used by the running interactive → 1 batch admitted
    assert sum(_status(b) == "running" for b in batch) == 1, [_status(b) for b in batch]
