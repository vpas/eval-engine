"""Unit: SWE-bench support — image naming, per-sample sandbox, the vendored grader, dataset shape.

No backends, no Docker, no network — the agent/sandbox execution is validated on the cluster. Here we
test the pure logic: the grading parsers (vendored from swebench, MIT) and the committed Lite subset.
"""
import os

import pytest

from eval_engine import builtins, plugins, swebench  # noqa: F401  populate registry
from eval_engine.datasets import load_jsonl

SWE = "examples/benchmarks/swebench.jsonl"


def test_registry_has_swe_bench():
    cat = {(p["kind"], p["name"]) for p in plugins.catalog()}
    assert ("harness", "swe_bench") in cat and ("scorer", "swe_bench") in cat
    assert callable(plugins.build("harness", {"type": "swe_bench"})[0])
    assert callable(plugins.build("scorer", {"type": "swe_bench"})[0])


def test_image_naming_rewrites_double_underscore():
    assert swebench.image_for("astropy__astropy-12907") == \
        "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"


def test_persample_sandbox_docker_and_k8s(monkeypatch, tmp_path):
    monkeypatch.setenv("EVAL_ENGINE_DATA", str(tmp_path))
    img = swebench.image_for("pallets__flask-1")
    monkeypatch.setenv("EVAL_ENGINE_SWE_SANDBOX", "docker")
    d = swebench.persample_sandbox(img)
    assert d.type == "docker" and img in (tmp_path / "swe-sandboxes").glob("*.compose.yaml").__next__().read_text()
    monkeypatch.setenv("EVAL_ENGINE_SWE_SANDBOX", "k8s")
    k = swebench.persample_sandbox(img)
    vals = (tmp_path / "swe-sandboxes").glob("*.values.yaml").__next__().read_text()
    assert k.type == "k8s" and "runtimeClassName: gvisor" in vals and img in vals


def test_extract_test_output_between_markers():
    log = f"pre\n{swebench.START_TEST_OUTPUT}\nMIDDLE\n{swebench.END_TEST_OUTPUT}\npost"
    assert swebench.extract_test_output(log).strip() == "MIDDLE"
    assert swebench.extract_test_output("no markers here") == "no markers here"


def _log(*lines):
    return f"{swebench.START_TEST_OUTPUT}\n" + "\n".join(lines) + f"\n{swebench.END_TEST_OUTPUT}"


def test_grade_resolved_when_all_pass():
    log = _log("PASSED t.py::a", "PASSED t.py::b", "PASSED t.py::c")
    r = swebench.grade(log, "pallets/flask", ["t.py::a"], ["t.py::b", "t.py::c"])
    assert r["resolved"] and r["fail_to_pass"] == [1, 1] and r["pass_to_pass"] == [2, 2]


def test_grade_unresolved_when_fail_to_pass_fails():
    log = _log("FAILED t.py::a - AssertionError", "PASSED t.py::b")
    r = swebench.grade(log, "pallets/flask", ["t.py::a"], ["t.py::b"])
    assert not r["resolved"] and r["fail_to_pass"] == [0, 1]


def test_grade_unresolved_when_regression_in_pass_to_pass():
    log = _log("PASSED t.py::a", "FAILED t.py::b - boom")
    r = swebench.grade(log, "pallets/flask", ["t.py::a"], ["t.py::b"])
    assert not r["resolved"] and r["pass_to_pass"] == [0, 1]


def test_grade_requires_nonempty_fail_to_pass():
    log = _log("PASSED t.py::b")
    assert not swebench.grade(log, "pallets/flask", [], ["t.py::b"])["resolved"]


def test_grade_pytest_options_parser_handles_parametrized_ids():
    # requests/pylint use the options parser (parametrized ids like test[opt]).
    log = _log("PASSED tests/test_x.py::test_q[case-1]")
    r = swebench.grade(log, "psf/requests", ["tests/test_x.py::test_q[case-1]"], [])
    assert r["resolved"]


# --- the committed Lite subset

def test_subset_shape_and_supported_repos():
    ds, _ = load_jsonl(SWE)
    samples = list(ds)
    assert len(samples) >= 10
    for s in samples:
        md = s.metadata or {}
        assert md["repo"] in swebench.SUPPORTED_REPOS         # grader covers it
        assert md.get("eval_script") and md.get("image", "").startswith("swebench/sweb.eval")
        assert isinstance(md.get("FAIL_TO_PASS"), list) and len(md["FAIL_TO_PASS"]) >= 1
        # per-sample sandbox attached from metadata.image
        assert s.sandbox is not None and s.sandbox.type in ("docker", "k8s")


def test_subset_spans_multiple_repos():
    ds, _ = load_jsonl(SWE)
    repos = {(s.metadata or {})["repo"] for s in ds}
    assert len(repos) >= 3
