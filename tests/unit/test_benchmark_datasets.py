"""Unit: the committed benchmark subsets are well-formed and match their harness/scorer contract.

These guard the generated JSONLs (examples/benchmarks/*.jsonl from tools/fetch_benchmark.py) so a bad
regeneration is caught before it reaches a run. No backends, no network — just the on-disk files.
"""
import pytest

from eval_engine import ifeval
from eval_engine.datasets import load_jsonl

BENCH = "examples/benchmarks"


def _load(name):
    ds, _ = load_jsonl(f"{BENCH}/{name}.jsonl")
    return list(ds)


@pytest.mark.parametrize("name,min_n", [
    ("gpqa", 40), ("mmlu", 40), ("gsm8k", 40), ("math500", 30), ("humaneval", 30), ("ifeval", 30),
])
def test_subset_nonempty(name, min_n):
    assert len(_load(name)) >= min_n


@pytest.mark.parametrize("name", ["gpqa", "mmlu"])
def test_multiple_choice_shape(name):
    for s in _load(name):
        assert s.choices and len(s.choices) == 4
        assert str(s.target) in ("A", "B", "C", "D")
        assert (s.metadata or {}).get("category")


def test_gsm8k_targets_are_numeric():
    for s in _load("gsm8k"):
        float(str(s.target).replace(",", ""))  # raises if not a number


def test_humaneval_carries_test_harness():
    for s in _load("humaneval"):
        md = s.metadata or {}
        assert md.get("test") and md.get("entry_point") and md.get("prompt")
        assert "def check" in md["test"]


def test_ifeval_only_supported_instructions():
    # Every instruction in the subset must have a verifier — else the score is silently wrong.
    for s in _load("ifeval"):
        md = s.metadata or {}
        ids = md.get("instruction_id_list") or []
        assert ids and all(iid in ifeval.SUPPORTED for iid in ids)
        assert len(md.get("kwargs") or []) == len(ids)


def test_mmlu_spans_multiple_categories():
    cats = {(s.metadata or {}).get("category") for s in _load("mmlu")}
    assert len(cats) >= 3  # the per-category UX needs >1 group_key
