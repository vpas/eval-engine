"""Unit: JSONL dataset loading — local file parsing only (no backends)."""
from eval_engine import datasets


def test_parses_input_target_metadata():
    ds, content_hash = datasets.load_jsonl("examples/qa.jsonl")
    assert len(ds.samples) == 3
    assert ds.samples[0].target == "Paris"
    assert len(content_hash) == 16  # content-addressed by sha256[:16]


def test_reads_multiple_choice_choices():
    ds, _ = datasets.load_jsonl("examples/mcq.jsonl")
    assert ds.samples[0].choices == ["London", "Paris", "Berlin", "Madrid"]
    assert ds.samples[0].target == "B"


def test_limit_truncates():
    ds, _ = datasets.load_jsonl("examples/qa.jsonl", limit=2)
    assert len(ds.samples) == 2
