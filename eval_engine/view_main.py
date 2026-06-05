"""Inspect log-viewer entrypoint, patched for our GKE-behind-OIDC deployment.

Two fixes, both needed to serve ``gs://`` logs through the ingress (ingress-nginx → oauth2-proxy →
this viewer):

1. **gcsfs ``datetime`` mtime** — some inspect builds compute ``mtime * 1000`` assuming a number, but
   ``gcsfs`` returns a ``datetime`` → ``TypeError`` when listing ``gs://`` logs. We normalize it to
   epoch seconds. Harmless (no-op) on builds that already handle this.

2. **Relative log names (the deep-link "Failed to fetch" fix)** — inspect's viewer client builds API
   URLs like ``/api/log-info/<urlencode(name)>``. When ``name`` is a full ``gs://…`` path, the encoded
   ``//`` becomes ``%2F%2F``; **oauth2-proxy** runs Go's ``path.Clean`` which decodes and collapses it
   to ``gs:/`` (via a 301), so the viewer 404s ("Failed to fetch") on *every* ``.eval`` read — not just
   the dashboard deep-link. We inject a ``FileMappingPolicy`` so the client only ever sees/sends a
   **relative basename** (``2026-…eval`` — no scheme, no ``/``): nothing for ``path.Clean`` to collapse,
   and the server maps it back to ``gs://<log_dir>/<name>`` to read+proxy the bytes server-side. The
   matching access policy validates the *mapped* (gs://) path against ``--log-dir``.

Usage (the inspect-view Deployment command):
    python -m eval_engine.view_main view start --host 0.0.0.0 --port 7575 --log-dir gs://… --recursive

The dashboard deep-link must pass the relative name + force the server API:
    /inspect/?log_file=<basename>&inspect_server=true
"""
import datetime as _dt
import sys

from inspect_ai._util import file as _f

# --- fix 1: gcsfs datetime mtime -------------------------------------------------------------------
_orig_file_info = _f.FileSystem._file_info


def _patched_file_info(self, info):  # type: ignore[no-untyped-def]
    mt = info.get("mtime")
    if isinstance(mt, _dt.datetime):
        info = {**info, "mtime": mt.timestamp()}
    return _orig_file_info(self, info)


_f.FileSystem._file_info = _patched_file_info


# --- fix 2: present relative log names so URLs carry no `//` for oauth2-proxy to collapse -----------
from inspect_ai._view import fastapi_server as _fs  # noqa: E402


class _RelMappingPolicy:
    """Map between the client-facing relative name and the real ``gs://<log_dir>/<name>`` path."""

    def __init__(self, log_dir: str) -> None:
        self.log_dir = log_dir.rstrip("/")
        self.prefix = self.log_dir + "/"

    def _to_server(self, file: str) -> str:
        if file.startswith("gs://") or file.startswith(self.log_dir):
            return file
        if file in ("", "."):
            return self.log_dir
        return self.prefix + file.lstrip("/")

    async def map(self, request, file: str) -> str:  # client → server  (noqa: ANN001)
        return self._to_server(file)

    async def unmap(self, request, file: str) -> str:  # server → client  (noqa: ANN001)
        if file.startswith(self.prefix):
            return file[len(self.prefix):]
        if file in (self.log_dir, self.log_dir + "/"):
            return ""
        return file


class _RelAccessPolicy:
    """Validate the (mapped) gs:// path stays under ``--log-dir``; accepts relative client names."""

    def __init__(self, log_dir: str) -> None:
        self._map = _RelMappingPolicy(log_dir)
        self.log_dir = self._map.log_dir

    def _ok(self, f: str) -> bool:
        s = self._map._to_server(f)
        return s.startswith(self.log_dir) and ".." not in s

    async def can_read(self, request, file: str) -> bool:    # noqa: ANN001
        return self._ok(file)

    async def can_delete(self, request, file: str) -> bool:  # noqa: ANN001
        return self._ok(file)

    async def can_list(self, request, dir: str) -> bool:     # noqa: ANN001
        return self._ok(dir)

    async def can_write(self, request, file: str) -> bool:   # noqa: ANN001
        return self._ok(file)


# The client expands a relative log name to an absolute path with `join(name, logDir)`, where
# `logDir` comes from GET /log-dir — and `join` returns the name unchanged when `logDir` is empty.
# That route never unmaps, so it returns the raw `gs://…` dir → the client rebuilds the full gs://
# path (with the `//` that oauth2-proxy collapses). We make /log-dir report an EMPTY dir for gs://,
# so `join(name, "")` keeps names relative end-to-end. Listing still uses the server's internal
# default_dir (the gs:// path) since the client omits the `log_dir` query param when logDir is empty.
_orig_get_log_dir = _fs.get_log_dir


def _patched_get_log_dir(log_dir):  # type: ignore[no-untyped-def]
    if isinstance(log_dir, str) and log_dir.startswith("gs://"):
        return _fs.LogDirResponse(log_dir="")
    return _orig_get_log_dir(log_dir)


_fs.get_log_dir = _patched_get_log_dir


_orig_view_server_app = _fs.view_server_app


def _view_server_app_with_mapping(*args, **kwargs):  # type: ignore[no-untyped-def]
    """``view_server`` calls this with ``mapping_policy=None`` + an ``OnlyDirAccessPolicy``. For a
    ``gs://`` log dir we substitute our relative-name mapping + matching access policy so the viewer
    works behind oauth2-proxy. Other (local) dirs are left untouched."""
    default_dir = kwargs.get("default_dir", "")
    if kwargs.get("mapping_policy") is None and isinstance(default_dir, str) and default_dir.startswith("gs://"):
        kwargs["mapping_policy"] = _RelMappingPolicy(default_dir)
        kwargs["access_policy"] = _RelAccessPolicy(default_dir)
    return _orig_view_server_app(*args, **kwargs)


_fs.view_server_app = _view_server_app_with_mapping


from inspect_ai._cli.main import main  # noqa: E402

if __name__ == "__main__":
    sys.argv = ["inspect", *sys.argv[1:]]
    sys.exit(main())
