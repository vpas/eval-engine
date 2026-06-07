"""Unit: the patched Inspect log-viewer policies (``eval_engine.view_main``) — pure, no backends.

The viewer runs behind ingress-nginx → oauth2-proxy. oauth2-proxy runs Go's ``path.Clean``, which
decodes ``%2F%2F`` and collapses ``gs://`` → ``gs:/`` (a 301) — so if the client ever sends a full
``gs://…`` log path the server 404s ("Failed to fetch") on EVERY ``.eval`` read. The fix keeps names
**relative** end-to-end: the client only ever sees/sends a bare basename (no scheme, no ``//`` for
``path.Clean`` to touch) and the server maps it back to ``gs://<log_dir>/<name>`` to read the bytes.
These tests pin that mapping + the matching access policy + the empty-dir ``/log-dir`` response.

(Importing the module monkeypatches inspect_ai internals at import — that's its job; we only assert the
pure policy logic here, never start a server.)
"""
from __future__ import annotations

import asyncio

from eval_engine import view_main as v

LOG_DIR = "gs://bucket/run-logs"
NAME = "2026-06-07T12-00-00_capitals.eval"
ABS = f"{LOG_DIR}/{NAME}"


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- mapping policy

def test_to_server_expands_relative_to_full_gs_path():
    m = v._RelMappingPolicy(LOG_DIR)
    assert m._to_server(NAME) == ABS
    assert m._to_server("/" + NAME) == ABS  # a leading slash is stripped, not doubled


def test_to_server_passes_through_already_absolute_paths():
    m = v._RelMappingPolicy(LOG_DIR)
    assert m._to_server(ABS) == ABS  # already gs:// — left unchanged (no double prefix)


def test_to_server_empty_and_dot_resolve_to_the_log_dir():
    m = v._RelMappingPolicy(LOG_DIR)
    assert m._to_server("") == LOG_DIR
    assert m._to_server(".") == LOG_DIR


def test_map_is_client_to_server_unmap_is_its_inverse():
    m = v._RelMappingPolicy(LOG_DIR)
    assert _run(m.map(None, NAME)) == ABS                 # client → server: expand
    assert _run(m.unmap(None, ABS)) == NAME               # server → client: strip prefix → relative
    # round-trips: a relative name survives map→unmap unchanged.
    assert _run(m.unmap(None, _run(m.map(None, NAME)))) == NAME


def test_unmap_collapses_the_dir_itself_to_empty():
    m = v._RelMappingPolicy(LOG_DIR)
    assert _run(m.unmap(None, LOG_DIR)) == ""
    assert _run(m.unmap(None, LOG_DIR + "/")) == ""


def test_trailing_slash_on_log_dir_is_normalized():
    m = v._RelMappingPolicy(LOG_DIR + "/")
    assert m.log_dir == LOG_DIR and m._to_server(NAME) == ABS


# --------------------------------------------------------------------------- access policy

def test_access_allows_names_under_the_log_dir():
    a = v._RelAccessPolicy(LOG_DIR)
    assert _run(a.can_read(None, NAME)) is True
    assert _run(a.can_list(None, "")) is True            # listing the (relative) root
    assert _run(a.can_write(None, NAME)) is True
    assert _run(a.can_delete(None, ABS)) is True


def test_access_rejects_path_traversal_escapes():
    a = v._RelAccessPolicy(LOG_DIR)
    assert _run(a.can_read(None, "../etc/passwd")) is False
    assert _run(a.can_read(None, "../../secret.eval")) is False


# --------------------------------------------------------------------------- /log-dir override

def test_get_log_dir_reports_empty_for_gs_so_client_keeps_names_relative():
    # The client does join(name, logDir); an empty logDir keeps `name` relative end-to-end (no `//`).
    assert v._patched_get_log_dir("gs://bucket/run-logs").log_dir == ""


def test_get_log_dir_leaves_local_dirs_untouched():
    # A local (non-gs://) dir doesn't hit oauth2-proxy's path.Clean, so it's reported verbatim.
    assert v._patched_get_log_dir("/var/log/inspect").log_dir == "/var/log/inspect"
