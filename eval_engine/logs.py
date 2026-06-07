"""Centralized logging — one configured stream so the log viewer actually shows what's happening.

Every module does ``from .logs import get_logger; log = get_logger(__name__)``. The first call wires
a single stdout handler onto the ``eval_engine`` parent logger (so the lines land in container stdout →
GCP Logging / the log viewer) and we never touch the Python root, so uvicorn's own access/error logs
are left alone. Level is ``EVAL_ENGINE_LOG_LEVEL`` (default INFO; set DEBUG to see per-batch detail).

Leveling convention used across the codebase, so the viewer's severity filter is meaningful:
  DEBUG   per-batch / per-tick mechanics (claims, reconcile timings) — noisy, off by default.
  INFO    lifecycle a human watching a run wants: launch, admit, batch result, finalize, anomaly.
  WARNING recoverable-but-notable: a sample retry, budget stop, price-fetch miss, leader handover.
  ERROR   something failed and was swallowed to keep a loop alive (a tick raised, all DB retries lost).

Output format (``EVAL_ENGINE_LOG_JSON``):
  - **text** (default) — the human-readable single-line format, for local dev / a TTY.
  - **json** — one JSON object per line via ``python-json-logger``, with a GCP-native ``severity`` field
    (Cloud Logging promotes ``severity`` + ``message`` out of the structured payload, so the Log
    Explorer severity filter + ``jsonPayload`` field queries work instead of substring matches).
    Auto-on in the cluster: defaults to JSON whenever ``EVAL_ENGINE_GCP_PROJECT`` is set, text otherwise.
"""
from __future__ import annotations

import logging
import os
import sys

_PARENT = "eval_engine"
_FORMAT = "%(asctime)s %(levelname)-5s %(name)s | %(message)s"
_configured = False


def _json_enabled() -> bool:
    """JSON when ``EVAL_ENGINE_LOG_JSON`` is set truthy; else auto-on in the cluster (GCP project set)."""
    v = os.environ.get("EVAL_ENGINE_LOG_JSON")
    if v is not None:
        return v.lower() in ("1", "true", "yes", "json", "on")
    return bool(os.environ.get("EVAL_ENGINE_GCP_PROJECT"))


def _formatter() -> logging.Formatter:
    if not _json_enabled():
        return logging.Formatter(_FORMAT, datefmt="%H:%M:%S")
    # python-json-logger (4.x: pythonjsonlogger.json). `severity` is what GCP Cloud Logging reads to set
    # the entry's LogSeverity (so the Log Explorer level filter works); `timestamp`/`logger` round it out.
    from pythonjsonlogger.json import JsonFormatter

    class _GcpJsonFormatter(JsonFormatter):
        def add_fields(self, log_record, record, message_dict):  # type: ignore[override]
            super().add_fields(log_record, record, message_dict)
            log_record["severity"] = record.levelname  # Python level names == GCP severities
            log_record["logger"] = record.name

    return _GcpJsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s",
                             rename_fields={"asctime": "timestamp", "message": "message"})


def setup(level: str | None = None) -> None:
    """Install the stdout handler on the ``eval_engine`` logger (idempotent). Safe to call from any
    entrypoint; ``get_logger`` calls it lazily so importing a module is enough to get logging."""
    global _configured
    lvl = (level or os.environ.get("EVAL_ENGINE_LOG_LEVEL", "INFO")).upper()
    parent = logging.getLogger(_PARENT)
    parent.setLevel(getattr(logging, lvl, logging.INFO))
    if not _configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_formatter())
        parent.addHandler(handler)
        parent.propagate = False  # don't double-emit through the Python root (uvicorn owns that)
        _configured = True


def get_logger(name: str) -> logging.Logger:
    setup()
    # The worker/orchestrator/training entrypoints run as `python -m eval_engine.<mod>`, so their
    # module __name__ is "__main__" — NOT a child of the `eval_engine` logger, so it would miss our
    # handler and fall through to logging.lastResort (unformatted, WARNING-only). Remap it back to its
    # real dotted name (`python -m` records it on the __main__ module's __spec__) so those processes
    # log through the same configured handler as every imported module.
    if name == "__main__":
        spec = getattr(sys.modules.get("__main__"), "__spec__", None)
        if spec and spec.name:
            name = spec.name
    # `name` is now the module's dotted path (e.g. "eval_engine.worker") → a child of the parent
    # logger, so it inherits the one handler + level configured above.
    return logging.getLogger(name)
