#!/usr/bin/env python3
"""Convert Petri's built-in seed instructions → our JSONL seed dataset (docs/PETRI.md §G3).

A Petri "seed" is a short instruction that tells the auditor what scenario/behavior to probe. In our
model a seed is just a Sample: ``input`` = the instruction text, ``metadata.category`` = the behavior
family (the seed's first tag) so the dashboard's per-``group_key`` breakdown becomes
**misalignment-by-behavior**. The result flows through the normal content-addressed snapshot path
(register via ``POST /datasets``), exactly like any other dataset — no live dependency at run time.

Requires the [petri] extra (``inspect_petri``) for its bundled seeds. Deterministic: stable id =
the seed's built-in id (filename stem); ``--limit`` takes the first N by id (sorted) for a reproducible
subset. Usage:

    python tools/fetch_petri_seeds.py --out examples/benchmarks/petri_seeds.jsonl --limit 8
    python tools/fetch_petri_seeds.py --tags deception,sycophancy   # subset by behavior tag
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def fetch(tags: list[str] | None, limit: int | None) -> list[dict]:
    try:
        from inspect_petri import seeds_dataset
    except ImportError as e:  # pragma: no cover
        raise SystemExit("inspect_petri not installed — `pip install 'eval-engine[petri]'`") from e
    dataset = seeds_dataset("tags:" + ",".join(tags) if tags else None)
    rows: list[dict] = []
    for s in dataset:
        meta = dict(s.metadata or {})
        seed_tags = meta.get("tags") or []
        rows.append({
            "id": str(s.id),
            "input": s.input if isinstance(s.input, str) else str(s.input),
            "target": "",  # alignment audits have no gold answer
            "metadata": {"category": (seed_tags[0] if seed_tags else "openended"), "tags": seed_tags},
        })
    rows.sort(key=lambda r: r["id"])  # deterministic order → reproducible subset
    return rows[:limit] if limit else rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="examples/benchmarks/petri_seeds.jsonl", type=Path)
    ap.add_argument("--tags", default="", help="comma-separated behavior tags to subset (OR-match)")
    ap.add_argument("--limit", type=int, default=None, help="first N seeds (sorted by id)")
    args = ap.parse_args()
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] or None
    rows = fetch(tags, args.limit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} Petri seeds → {args.out}")


if __name__ == "__main__":
    main()
