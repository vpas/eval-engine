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

from . import storage

# Content-addressed snapshot store (SCHEMA §0, DESIGN §13): immutable copies keyed by content hash.
# GCS in-cluster, a local dir in dev — both via the fsspec storage abstraction (DESIGN §4, #14).
GCS_BUCKET = os.environ.get("EVAL_ENGINE_GCS_BUCKET")
SNAPSHOT_DIR = Path(os.environ.get("EVAL_ENGINE_DATA", ".data")) / "datasets"


def snapshot(uri: str) -> tuple[str, str]:
    """Content-address a dataset (FR1, §13): hash its bytes and write an IMMUTABLE copy to object
    storage keyed by the hash (write-once — same content → same key, never overwritten). Returns
    (content_hash, snapshot_uri). The registrar pins both on the DatasetSpec so the version is
    reproducible by content rather than by a mutable path."""
    raw = storage.read_bytes(uri)
    h = hashlib.sha256(raw).hexdigest()[:16]
    tail = uri.rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1] if "." in tail else "jsonl"
    snap = f"gs://{GCS_BUCKET}/datasets/{h}.{ext}" if GCS_BUCKET else str(SNAPSHOT_DIR / f"{h}.{ext}")
    if not storage.exists(snap):  # write-once
        storage.write_bytes(snap, raw)
    return h, snap


def load_jsonl(path: str, limit: int | None = None) -> tuple[MemoryDataset, str]:
    raw = storage.read_bytes(path)  # local path or gs:// snapshot
    content_hash = hashlib.sha256(raw).hexdigest()[:16]

    samples: list[Sample] = []
    for i, line in enumerate(raw.decode().splitlines()):
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        meta = rec.get("metadata", {})
        sample = Sample(
            id=str(rec.get("id", i)),
            input=rec["input"],
            target=rec.get("target", ""),
            choices=rec.get("choices"),  # multiple_choice harness (a lettered choice list)
            metadata=meta,
        )
        # Per-sample sandbox (SWE-bench): each instance runs in its own official image. When a sample
        # carries metadata.image, attach its sandbox here so the agent + scorer share that container
        # (k8s/docker per EVAL_ENGINE_SWE_SANDBOX). Inspect uses a sample's sandbox over the task's.
        if meta.get("image"):
            from . import swebench  # lazy: only SWE-bench samples need it
            sample.sandbox = swebench.persample_sandbox(meta["image"])
        samples.append(sample)
    if limit:
        samples = samples[:limit]
    return MemoryDataset(samples), content_hash
