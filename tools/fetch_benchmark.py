#!/usr/bin/env python3
"""Fetch + subsample real benchmarks → our JSONL schema (docs/ADDING_REAL_EVALS.md §G1).

A **one-shot ingestion tool**, not a runtime dependency: it pulls each benchmark from HuggingFace,
deterministically subsamples it (fixed seed → the same items every run, so the demo is reproducible),
maps the fields into our ``{id, input, target, choices, metadata}`` shape, and writes a small JSONL into
``examples/benchmarks/``. Those JSONLs are committed and then registered via ``POST /datasets`` (which
content-addresses + snapshots them), so runs execute against pinned content with no HF access.

    pip install datasets        # tooling only; the engine never imports `datasets`
    python tools/fetch_benchmark.py            # all benchmarks
    python tools/fetch_benchmark.py gsm8k mmlu # a subset

`metadata.category` becomes the ClickHouse ``group_key`` (the per-category dashboard breakdown) — for
MMLU it's the subject, so the breakdown lights up across subjects.

Licenses (recorded in examples/benchmarks/README.md): GSM8K MIT · MMLU MIT · MATH MIT · HumanEval MIT ·
IFEval Apache-2.0 · GPQA CC-BY (canonical set is gated; we use an open Diamond mirror).
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "examples" / "benchmarks"
SEED = 20240606  # fixed → reproducible subsets

# IFEval: only sample prompts whose every instruction has a verifier in eval_engine/ifeval.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eval_engine.ifeval import SUPPORTED as IFEVAL_SUPPORTED  # noqa: E402


def _load(path, config=None, split="test"):
    from datasets import load_dataset
    return load_dataset(path, config, split=split)


def _subsample(n: int, k: int) -> list[int]:
    """Deterministic sorted index subset (same seed → same items)."""
    if k >= n:
        return list(range(n))
    return sorted(random.Random(SEED).sample(range(n), k))


# --------------------------------------------------------------------------- builders
# Each returns a list of records in our schema.

def _parse_gpqa(q: str, ans: str) -> tuple[str, list[str], str] | None:
    """The open `fingertap/GPQA-diamond` mirror inlines options inconsistently: every row has an
    'A. … B. … C. … D. …' block (the answer letter indexes it), but some rows make those uppercase
    options *pointers* into a lowercase 'a) … b) …' list. Resolve both → (stem, 4 choices, letter)."""
    low = dict(re.findall(r"(?m)^\s*([a-d])\)\s*(.+?)\s*$", q))
    up = re.findall(r"(?m)^\s*([A-D])\.\s*(.+?)\s*$", q)
    if len(up) != 4 or ans not in "ABCD":
        return None
    choices = []
    for _letter, txt in up:
        t = txt.strip()
        if len(t) == 1 and t.lower() in low:  # uppercase option is a pointer into the lowercase list
            t = low[t.lower()]
        choices.append(t)
    stem = re.split(r"(?m)^\s*[A-Da-d][.)]\s+", q)[0].strip()
    return stem, choices, ans


def build_gpqa(k: int) -> list[dict]:
    """GPQA Diamond (hard graduate-science MCQ) → multiple_choice harness + choice scorer."""
    ds = _load("fingertap/GPQA-diamond", split="test")
    out = []
    for i in _subsample(len(ds), k):
        parsed = _parse_gpqa(ds[i]["question"], str(ds[i]["answer"]).strip().upper())
        if not parsed:
            continue
        stem, choices, ans = parsed
        out.append({"id": f"gpqa-{i}", "input": stem, "choices": choices, "target": ans,
                    "metadata": {"category": "gpqa"}})
    return out


def build_mmlu(k: int) -> list[dict]:
    """MMLU across a few subjects → the per-category UX breakdown. Splits k across subjects."""
    subjects = ["high_school_mathematics", "professional_law", "college_biology",
                "high_school_world_history"]
    per = max(1, k // len(subjects))
    out = []
    for subj in subjects:
        ds = _load("cais/mmlu", subj, split="test")
        for i in _subsample(len(ds), per):
            row = ds[i]
            out.append({"id": f"mmlu-{subj}-{i}", "input": row["question"],
                        "choices": list(row["choices"]), "target": "ABCD"[int(row["answer"])],
                        "metadata": {"category": subj}})
    return out


def build_gsm8k(k: int) -> list[dict]:
    """GSM8K grade-school math → free-form numeric. Target = the number after '####'."""
    ds = _load("openai/gsm8k", "main", split="test")
    out = []
    for i in _subsample(len(ds), k):
        row = ds[i]
        answer = row["answer"].split("####")[-1].strip().replace(",", "")
        out.append({"id": f"gsm8k-{i}", "input": row["question"], "target": answer,
                    "metadata": {"category": "gsm8k"}})
    return out


def build_math500(k: int) -> list[dict]:
    """MATH-500 competition math → symbolic-equivalence scoring. Target = the bare answer."""
    ds = _load("HuggingFaceH4/MATH-500", split="test")
    out = []
    for i in _subsample(len(ds), k):
        row = ds[i]
        out.append({"id": f"math-{i}", "input": row["problem"], "target": row["answer"],
                    "metadata": {"category": str(row.get("subject", "math")),
                                 "level": row.get("level")}})
    return out


def build_humaneval(k: int) -> list[dict]:
    """HumanEval code generation → sandbox execution. Carry the test harness + entry point in metadata."""
    ds = _load("openai/openai_humaneval", split="test")
    out = []
    for i in _subsample(len(ds), k):
        row = ds[i]
        out.append({"id": row["task_id"].replace("/", "-"), "input": row["prompt"], "target": "",
                    "metadata": {"category": "humaneval", "prompt": row["prompt"],
                                 "test": row["test"], "entry_point": row["entry_point"]}})
    return out


def build_ifeval(k: int) -> list[dict]:
    """IFEval instruction-following. Keep only prompts whose every instruction has a verifier we
    implement (honest narrowing). Strip None kwargs so the stored sample is clean."""
    ds = _load("google/IFEval", split="train")
    out = []
    for i in range(len(ds)):
        row = ds[i]
        ids = list(row["instruction_id_list"])
        if not ids or any(iid not in IFEVAL_SUPPORTED for iid in ids):
            continue
        kwargs = [{kk: vv for kk, vv in (kw or {}).items() if vv is not None} for kw in row["kwargs"]]
        out.append({"id": f"ifeval-{row['key']}", "input": row["prompt"], "target": "",
                    "metadata": {"category": "ifeval", "instruction_id_list": ids, "kwargs": kwargs}})
    # subsample the eligible pool deterministically
    keep = _subsample(len(out), k)
    return [out[j] for j in keep]


def build_swebench(k: int) -> list[dict]:
    """SWE-bench Lite (agentic software engineering). Narrowed to the PyTest-family repos our vendored
    grader covers (eval_engine/swebench.py:SUPPORTED_REPOS) — honest, like the IFEval narrowing. For
    each instance we precompute the **self-contained eval_script** (applies the held-out test patch +
    runs the repo's tests) with the real `swebench` package here (tooling-only), and embed it so the
    runtime needs no swebench/torch deps. The per-instance official image is recorded for the sandbox."""
    import json as _json
    from swebench.harness.test_spec.test_spec import make_test_spec
    from eval_engine.swebench import SUPPORTED_REPOS, image_for

    ds = _load("princeton-nlp/SWE-bench_Lite", split="test")
    eligible = [i for i in range(len(ds)) if ds[i]["repo"] in SUPPORTED_REPOS]
    out = []
    for i in (eligible if len(eligible) <= k else [eligible[j] for j in _subsample(len(eligible), k)]):
        r = ds[i]
        f2p = r["FAIL_TO_PASS"] if isinstance(r["FAIL_TO_PASS"], list) else _json.loads(r["FAIL_TO_PASS"])
        p2p = r["PASS_TO_PASS"] if isinstance(r["PASS_TO_PASS"], list) else _json.loads(r["PASS_TO_PASS"])
        eval_script = make_test_spec(dict(r)).eval_script
        out.append({"id": r["instance_id"], "input": r["problem_statement"], "target": "",
                    "metadata": {"category": r["repo"], "repo": r["repo"],
                                 "instance_id": r["instance_id"], "image": image_for(r["instance_id"]),
                                 "FAIL_TO_PASS": f2p, "PASS_TO_PASS": p2p, "eval_script": eval_script}})
    return out


BUILDERS = {
    "gpqa": (build_gpqa, 50),
    "swebench": (build_swebench, 15),
    "mmlu": (build_mmlu, 60),
    "gsm8k": (build_gsm8k, 50),
    "math500": (build_math500, 40),
    "humaneval": (build_humaneval, 40),
    "ifeval": (build_ifeval, 50),
}


def write_jsonl(name: str, records: list[dict]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.jsonl"
    with path.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("benchmarks", nargs="*", default=[],
                    help=f"which to fetch (default: all). one of: {', '.join(BUILDERS)}")
    ap.add_argument("--limit", type=int, default=None, help="override per-benchmark subset size")
    args = ap.parse_args()
    names = args.benchmarks or list(BUILDERS)
    for name in names:
        builder, default_k = BUILDERS[name]
        k = args.limit or default_k
        print(f"▶ {name}: fetching + subsampling to {k} …", flush=True)
        records = builder(k)
        path = write_jsonl(name, records)
        cats = sorted({r["metadata"].get("category", "") for r in records})
        print(f"  ✓ wrote {len(records)} → {path.relative_to(OUT_DIR.parent.parent)}  categories={cats}")


if __name__ == "__main__":
    main()
