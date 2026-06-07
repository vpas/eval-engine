"""Unit: the orphaned-sandbox reaper's pure age policy (eval_engine/ops._orphan_releases).

The full reaper shells out to helm against a live cluster, but its decision — *which* per-sample
sandbox releases are old enough to be leaks — is a pure function of {release: age_s} and the cutoff.
A release is reaped iff it has lived past the cutoff (which sits above any live sample's whole
wall-clock), so reaping can never race a still-running sandbox.
"""
from eval_engine.ops import _orphan_releases


def test_young_releases_are_kept():
    # Everything below the cutoff is a (possibly) live sandbox → never reaped.
    ages = {"a": 10.0, "b": 599.0, "c": 0.0}
    assert _orphan_releases(ages, cutoff_s=1800.0) == []


def test_old_releases_are_reaped_oldest_first():
    ages = {"fresh": 100.0, "old": 4000.0, "ancient": 9000.0, "borderline": 1801.0}
    # only those at/over the cutoff, most-stranded (oldest) first
    assert _orphan_releases(ages, cutoff_s=1800.0) == ["ancient", "old", "borderline"]


def test_exactly_at_cutoff_is_reaped():
    # boundary is inclusive: a release that has reached the cutoff is an orphan.
    assert _orphan_releases({"x": 1800.0}, cutoff_s=1800.0) == ["x"]
    assert _orphan_releases({"x": 1799.999}, cutoff_s=1800.0) == []


def test_empty_input():
    assert _orphan_releases({}, cutoff_s=1800.0) == []
