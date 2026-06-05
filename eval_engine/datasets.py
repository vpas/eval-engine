"""JSONL dataset loader → Inspect ``MemoryDataset``.

Prototype of the content-addressed snapshot idea (SCHEMA §0): we hash the raw bytes so a
dataset version is pinned by content. Production loads from the snapshot URI; here from a file.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from inspect_ai.dataset import MemoryDataset, Sample

# Content-addressed snapshot store (SCHEMA §0, DESIGN §13): immutable copies keyed by content hash.
# GCS in-cluster, a local dir in dev — the S3-API abstraction that removes the native GCS calls is a
# separate Tier-3 item (#14); this mirrors the existing runner.py pattern for now.
GCS_BUCKET = os.environ.get("EVAL_ENGINE_GCS_BUCKET")
SNAPSHOT_DIR = Path(os.environ.get("EVAL_ENGINE_DATA", ".data")) / "datasets"
_gcs_client = None


def _gcs():
    global _gcs_client
    if _gcs_client is None:
        from google.cloud import storage  # ADC = the GKE node SA
        _gcs_client = storage.Client()
    return _gcs_client


def _read_bytes(uri: str) -> bytes:
    """Read a dataset's bytes from a local path or a ``gs://`` URI (so runs can load a snapshot)."""
    if uri.startswith("gs://"):
        bucket, _, key = uri[len("gs://"):].partition("/")
        return _gcs().bucket(bucket).blob(key).download_as_bytes()
    return Path(uri).read_bytes()


def snapshot(uri: str) -> tuple[str, str]:
    """Content-address a dataset (FR1, §13): hash its bytes and write an IMMUTABLE copy to object
    storage keyed by the hash (write-once — same content → same key, never overwritten). Returns
    (content_hash, snapshot_uri). The registrar pins both on the DatasetSpec so the version is
    reproducible by content rather than by a mutable path."""
    raw = _read_bytes(uri)
    h = hashlib.sha256(raw).hexdigest()[:16]
    tail = uri.rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1] if "." in tail else "jsonl"
    if GCS_BUCKET:
        key = f"datasets/{h}.{ext}"
        blob = _gcs().bucket(GCS_BUCKET).blob(key)
        if not blob.exists():
            blob.upload_from_string(raw)
        return h, f"gs://{GCS_BUCKET}/{key}"
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    p = SNAPSHOT_DIR / f"{h}.{ext}"
    if not p.exists():
        p.write_bytes(raw)
    return h, str(p)


def load_jsonl(path: str, limit: int | None = None) -> tuple[MemoryDataset, str]:
    raw = _read_bytes(path)  # local path or gs:// snapshot
    content_hash = hashlib.sha256(raw).hexdigest()[:16]

    samples: list[Sample] = []
    for i, line in enumerate(raw.decode().splitlines()):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        samples.append(
            Sample(
                id=str(rec.get("id", i)),
                input=rec["input"],
                target=rec.get("target", ""),
                choices=rec.get("choices"),  # multiple_choice harness (a lettered choice list)
                metadata=rec.get("metadata", {}),
            )
        )
    if limit:
        samples = samples[:limit]
    return MemoryDataset(samples), content_hash
