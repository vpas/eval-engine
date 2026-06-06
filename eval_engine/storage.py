"""Object storage via fsspec — one interface for local paths and remote URIs (``gs://``, ``s3://``…).

DESIGN §4 ("portability"): app code must not call a cloud-vendor SDK directly. fsspec gives a single
API where the URI **scheme selects the driver** — ``gcsfs`` for ``gs://`` in this deployment, ``s3fs``
for ``s3://`` — with no code change, so the object store is swappable. Credentials come from the
environment (ADC = the GKE node service account for ``gs://``); a bare path is the local filesystem.

This is the only module that touches the storage layer; ``runner`` (transcripts) and ``datasets``
(snapshots) go through it. Inspect's own ``.eval`` logs already use fsspec internally.
"""
from __future__ import annotations

import os

import fsspec
from fsspec.core import url_to_fs
from fsspec.implementations.local import LocalFileSystem


def read_bytes(uri: str) -> bytes:
    with fsspec.open(uri, "rb") as f:
        return f.read()


def read_text(uri: str) -> str:
    return read_bytes(uri).decode()


def write_bytes(uri: str, data: bytes) -> None:
    fs, path = url_to_fs(uri)
    # Object stores have no real directories; the local filesystem does, so make parents first.
    if isinstance(fs, LocalFileSystem):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with fs.open(path, "wb") as f:
        f.write(data)


def write_text(uri: str, text: str) -> None:
    write_bytes(uri, text.encode())


def exists(uri: str) -> bool:
    fs, path = url_to_fs(uri)
    return fs.exists(path)
