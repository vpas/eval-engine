"""JSONL dataset loader → Inspect ``MemoryDataset``.

Prototype of the content-addressed snapshot idea (SCHEMA §0): we hash the raw bytes so a
dataset version is pinned by content. Production loads from the snapshot URI; here from a file.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from inspect_ai.dataset import MemoryDataset, Sample


def load_jsonl(path: str, limit: int | None = None) -> tuple[MemoryDataset, str]:
    raw = Path(path).read_bytes()
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
                metadata=rec.get("metadata", {}),
            )
        )
    if limit:
        samples = samples[:limit]
    return MemoryDataset(samples), content_hash
