"""Unit: transcript retention — zstd compression + stratified sampling (DESIGN §13, #15)."""
import json

from eval_engine import runner
from eval_engine.models import PluginRef, RunSpec


def _spec(**over) -> RunSpec:
    base = dict(eval="e", dataset="d", harness=PluginRef(type="single_turn"), scorers=[])
    base.update(over)
    return RunSpec(**base)


def test_keep_all_when_rate_unset_or_one():
    assert runner._keep_transcript(_spec(), "s1", passed=1) is True          # None ⇒ keep all
    assert runner._keep_transcript(_spec(transcript_sample_rate=1.0), "s1", 1) is True


def test_failures_always_kept_even_when_sampling():
    # rate 0 keeps no passes, but failures (passed==0) are always retained — that's what you debug.
    assert runner._keep_transcript(_spec(transcript_sample_rate=0.0), "s1", passed=0) is True
    assert runner._keep_transcript(_spec(transcript_sample_rate=0.0), "s1", passed=1) is False


def test_sampling_is_deterministic_and_fractional():
    # Same id ⇒ same decision; at rate 0.5 roughly half of a large id space is kept.
    ids = [f"s{i:05d}" for i in range(2000)]
    kept = [i for i in ids if runner._keep_transcript(_spec(transcript_sample_rate=0.5), i, passed=1)]
    assert 0.4 < len(kept) / len(ids) < 0.6, len(kept) / len(ids)
    # stable across calls
    assert all(runner._keep_transcript(_spec(transcript_sample_rate=0.5), i, 1) in (True, False) for i in ids)
    a = runner._keep_transcript(_spec(transcript_sample_rate=0.5), "s00042", 1)
    b = runner._keep_transcript(_spec(transcript_sample_rate=0.5), "s00042", 1)
    assert a == b


def test_env_default_rate(monkeypatch):
    # A RunSpec with no rate falls back to the env default.
    monkeypatch.setattr(runner, "TRANSCRIPT_SAMPLE_RATE", 0.0)
    assert runner._keep_transcript(_spec(), "s1", passed=1) is False   # passes dropped
    assert runner._keep_transcript(_spec(), "s1", passed=0) is True    # failures kept
    assert runner._keep_transcript(_spec(transcript_sample_rate=1.0), "s1", 1) is True  # per-run override


def test_zstd_roundtrip(tmp_path, monkeypatch):
    # _put_transcript compresses to .json.zst; get_transcript decompresses back to the same JSON.
    monkeypatch.setattr(runner, "GCS_BUCKET", None)
    monkeypatch.setattr(runner, "TRANSCRIPTS", tmp_path)
    payload = {"input": "Q", "output": "Paris", "target": "Paris", "scores": {"includes": 1.0}}
    uri = runner._put_transcript("run1", "s1", payload)
    assert uri.endswith(".json.zst")
    assert json.loads(runner.get_transcript(uri)) == payload