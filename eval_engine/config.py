"""Shared, env-derived tunables read by more than one module.

Each value is defined once here so a default can't silently diverge between two readers — e.g. the
orchestrator that *enforces* the admission cap and the ops dashboard that *displays* it, or the
orchestrator/monitor that share the stale-leader threshold (docs/REFACTORING.md §6). Knobs read in a
single module stay in that module (the worker lease-renew interval, the monitor tick, the retry
policy, …); this file is only for the cross-module ones.
"""
from __future__ import annotations

import os

# Two-lane admission (SCHEDULER §2): a global cap on concurrently-running runs + a reserved interactive
# slice batch can borrow only when there's no interactive demand. Enforced by the orchestrator
# (_admit), displayed by the ops dashboard.
GLOBAL_MAX_RUNNING = int(os.environ.get("EVAL_ENGINE_GLOBAL_MAX_RUNNING", "50"))
INTERACTIVE_RESERVE = int(os.environ.get("EVAL_ENGINE_INTERACTIVE_RESERVE", "12"))  # ~25% of the cap

# Loop cadences. ORCH_TICK_SECONDS: the orchestrator tick interval + the ops orchestrator-staleness
# window (a heartbeat older than a few ticks is stale). WORKER_POLL_SECONDS: the worker's idle poll.
ORCH_TICK_SECONDS = float(os.environ.get("EVAL_ENGINE_ORCH_TICK", "2.0"))
WORKER_POLL_SECONDS = float(os.environ.get("EVAL_ENGINE_WORKER_POLL", "1.0"))

# Leader election: how long an idle advisory-lock holder is treated as crashed (bug B1). Shared by the
# orchestrator and the training monitor (control.run_as_leader takes it as stale_seconds).
STALE_LEADER_SECONDS = float(os.environ.get("EVAL_ENGINE_STALE_LEADER_SECONDS", "20"))
