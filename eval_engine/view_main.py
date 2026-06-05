"""Inspect log-viewer entrypoint with a fix for an inspect-ai + gcsfs incompatibility.

Some inspect-ai builds compute a file's mtime as ``mtime * 1000`` assuming a numeric value, but
``gcsfs`` returns ``mtime`` as a ``datetime`` → ``TypeError`` when LISTING ``gs://`` logs (which the
viewer does on load). We normalize a ``datetime`` mtime to epoch seconds before inspect handles it,
so the viewer can list GCS logs. Version-proof: harmless on builds that already handle this.

Usage (the inspect-view Deployment command):
    python -m eval_engine.view_main view start --host 0.0.0.0 --port 7575 --log-dir gs://… --recursive
"""
import datetime as _dt
import sys

from inspect_ai._util import file as _f

_orig_file_info = _f.FileSystem._file_info


def _patched_file_info(self, info):  # type: ignore[no-untyped-def]
    mt = info.get("mtime")
    if isinstance(mt, _dt.datetime):
        info = {**info, "mtime": mt.timestamp()}
    return _orig_file_info(self, info)


_f.FileSystem._file_info = _patched_file_info

from inspect_ai._cli.main import main  # noqa: E402

if __name__ == "__main__":
    sys.argv = ["inspect", *sys.argv[1:]]
    sys.exit(main())
