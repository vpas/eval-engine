"""Unit: the committed benchmark RunSpec YAMLs are valid and their plugins resolve.

Guards examples/benchmarks/*.yaml (the CLI launch path) + the tools/seed_benchmarks.py SUITE (the
dashboard registration path) so the two front doors stay consistent. No backends/network.
"""
import sys
from pathlib import Path

import pytest
import yaml

from eval_engine import builtins, plugins  # noqa: F401  populate the registry
from eval_engine.models import RunSpec

REPO = Path(__file__).resolve().parents[2]
YAML_DIR = REPO / "examples" / "benchmarks"
YAMLS = sorted(YAML_DIR.glob("*.yaml"))

sys.path.insert(0, str(REPO / "tools"))
import seed_benchmarks  # noqa: E402


@pytest.mark.parametrize("path", YAMLS, ids=lambda p: p.stem)
def test_yaml_is_a_valid_runspec_with_resolvable_plugins(path):
    spec = RunSpec(**yaml.safe_load(path.read_text()))
    # dataset file exists (the JSONL the run will load)
    assert (REPO / spec.dataset).exists()
    # harness + every scorer build from the registry
    built, _ = plugins.build("harness", spec.harness.model_dump())
    assert built is not None
    for s in spec.scorers:
        assert plugins.build("scorer", s.model_dump())[0] is not None


def test_one_yaml_per_benchmark():
    # YAML files are named by dataset stem; SUITE is keyed by eval id → its dataset stem.
    suite_stems = {stem for stem, _h, _s in seed_benchmarks.SUITE.values()}
    assert {p.stem for p in YAMLS} == suite_stems


def test_seed_suite_plugins_resolve_and_datasets_exist():
    for eval_id, (stem, harness, scorers) in seed_benchmarks.SUITE.items():
        assert (YAML_DIR / f"{stem}.jsonl").exists()
        assert plugins.build("harness", harness)[0] is not None
        for s in scorers:
            assert plugins.build("scorer", s)[0] is not None


def test_humaneval_uses_sandbox_harness_and_batch_one():
    spec = RunSpec(**yaml.safe_load((YAML_DIR / "humaneval.yaml").read_text()))
    built, _ = plugins.build("harness", spec.harness.model_dump())
    assert isinstance(built, tuple)  # (solver, sandbox) — declares a sandbox for code_exec
    assert spec.batch_size == 1      # one sample per Inspect eval ⇒ one sandbox
