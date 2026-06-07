"""SWE-bench support: per-instance sandbox + canonical test-log grading (docs/ADDING_REAL_EVALS.md).

A SWE-bench instance = a real GitHub issue + the repo at a base commit. The agent edits the repo
**inside the instance's official Docker image** (`swebench/sweb.eval.x86_64.<id>`, repo at /testbed);
scoring applies the held-out test patch and runs the repo's tests, resolving iff the FAIL_TO_PASS tests
now pass and the PASS_TO_PASS tests still pass.

Two halves live here:
- ``persample_sandbox(image)`` — the per-sample sandbox spec pointing at the instance image (k8s in
  cluster, docker locally). Driven by ``EVAL_ENGINE_SWE_SANDBOX`` (default docker).
- ``grade(log, repo, fail_to_pass, pass_to_pass)`` — grade the test log (returns a report dict whose
  ``resolved`` key is the RESOLVED_FULL verdict). The per-repo
  PyTest log parsers are **vendored from SWE-bench (MIT)** so the *runtime* needs no heavy swebench/
  torch/datasets deps; the converter (tools/fetch_benchmark.py) uses the real ``swebench`` package
  (tooling-only) to precompute each instance's self-contained ``eval_script``, embedded in the dataset.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from inspect_ai.util import SandboxEnvironmentSpec

# Markers the SWE-bench eval_script prints around the test command (swebench.harness.constants).
START_TEST_OUTPUT = ">>>>> Start Test Output"
END_TEST_OUTPUT = ">>>>> End Test Output"

# Where the repo lives inside every SWE-bench instance image, and the conda env to activate.
TESTBED = "/testbed"

def _generated_dir() -> Path:
    # Where generated per-instance sandbox specs live (read env at call time so tests can redirect it).
    return Path(os.environ.get("EVAL_ENGINE_DATA", ".data")) / "swe-sandboxes"


# --------------------------------------------------------------------------- per-sample sandbox

def image_for(instance_id: str) -> str:
    """Official SWE-bench eval image for an instance (Docker Hub). The instance id's `__` separator is
    rewritten to `_1776_` in the image tag (SWE-bench's convention)."""
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest"


def persample_sandbox(image: str) -> SandboxEnvironmentSpec:
    """Build the sandbox spec that runs one SWE-bench instance in its official image. k8s in cluster
    (per-sample gVisor pod), docker locally — selected by ``EVAL_ENGINE_SWE_SANDBOX`` (default docker).
    A small values/compose file is generated per image (content-addressed, write-once)."""
    kind = os.environ.get("EVAL_ENGINE_SWE_SANDBOX", "docker")
    gen = _generated_dir()
    gen.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256(image.encode()).hexdigest()[:12]
    if kind == "k8s":
        # inspect-k8s-sandbox helm values: one service on the gVisor runtime, kept alive, cwd=/testbed.
        path = gen / f"{h}.values.yaml"
        if not path.exists():
            path.write_text(
                "services:\n"
                "  default:\n"
                "    runtimeClassName: gvisor\n"
                f'    image: "{image}"\n'
                '    command: ["tail", "-f", "/dev/null"]\n'
                f"    workingDir: {TESTBED}\n"
            )
        return SandboxEnvironmentSpec("k8s", str(path))
    # local docker compose
    path = gen / f"{h}.compose.yaml"
    if not path.exists():
        path.write_text(
            "services:\n"
            "  default:\n"
            f"    image: {image}\n"
            '    command: ["tail", "-f", "/dev/null"]\n'
            "    init: true\n"
            f"    working_dir: {TESTBED}\n"
        )
    return SandboxEnvironmentSpec("docker", str(path))


# --------------------------------------------------------------------------- grading
# PyTest log parsers vendored from SWE-bench (swebench/harness/log_parsers/python.py, MIT). They map a
# test id -> status ("PASSED"/"FAILED"/...). We narrow our Lite subset (tools/fetch_benchmark.py) to
# repos these cover, so grading stays faithful without the runtime swebench dependency.

_STATUSES = ("FAILED", "PASSED", "SKIPPED", "ERROR", "XFAIL")


def _parse_pytest(log: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in log.split("\n"):
        if any(line.startswith(s) for s in _STATUSES):
            if line.startswith("FAILED"):
                line = line.replace(" - ", " ")
            parts = line.split()
            if len(parts) > 1:
                out[parts[1]] = parts[0]
    return out


def _parse_pytest_v2(log: str) -> dict[str, str]:
    out: dict[str, str] = {}
    escapes = "".join(chr(c) for c in range(1, 32))
    trans = str.maketrans("", "", escapes)
    for line in log.split("\n"):
        line = re.sub(r"\[(\d+)m", "", line).translate(trans)
        if any(line.startswith(s) for s in _STATUSES):
            if line.startswith("FAILED"):
                line = line.replace(" - ", " ")
            parts = line.split()
            if len(parts) >= 2:
                out[parts[1]] = parts[0]
        elif any(line.endswith(s) for s in _STATUSES):
            parts = line.split()
            if len(parts) >= 2:
                out[parts[0]] = parts[1]
    return out


_OPTION_RE = re.compile(r"(.*?)\[(.*)\]")


def _parse_pytest_options(log: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in log.split("\n"):
        if any(line.startswith(s) for s in _STATUSES):
            if line.startswith("FAILED"):
                line = line.replace(" - ", " ")
            parts = line.split()
            if len(parts) <= 1:
                continue
            m = _OPTION_RE.search(parts[1])
            if m:
                main, option = m.groups()
                if option.startswith("/") and not option.startswith("//") and "*" not in option:
                    option = "/" + option.split("/")[-1]
                name = f"{main}[{option}]"
            else:
                name = parts[1]
            out[name] = parts[0]
    return out


# repo -> parser (the PyTest-family repos in SWE-bench Lite; see tools/fetch_benchmark.py narrowing)
MAP_REPO_TO_PARSER = {
    "pytest-dev/pytest": _parse_pytest,
    "pydata/xarray": _parse_pytest,
    "pallets/flask": _parse_pytest,
    "scikit-learn/scikit-learn": _parse_pytest_v2,
    "sphinx-doc/sphinx": _parse_pytest_v2,
    "astropy/astropy": _parse_pytest_v2,
    "psf/requests": _parse_pytest_options,
    "pylint-dev/pylint": _parse_pytest_options,
}
SUPPORTED_REPOS = set(MAP_REPO_TO_PARSER)


def extract_test_output(log: str) -> str:
    """The slice of the eval log between the SWE-bench start/end markers (the actual test run)."""
    start = log.find(START_TEST_OUTPUT)
    end = log.find(END_TEST_OUTPUT)
    if start != -1 and end != -1 and end > start:
        return log[start + len(START_TEST_OUTPUT):end]
    return log  # markers absent (e.g. the script failed early) → parse whatever we got


def grade(log: str, repo: str, fail_to_pass: list[str], pass_to_pass: list[str]) -> dict:
    """Resolve a SWE-bench instance from the eval-script log (SWE-bench RESOLVED_FULL criterion):
    every FAIL_TO_PASS test now PASSED *and* every PASS_TO_PASS test still PASSED. Returns a report
    dict with the booleans + per-bucket pass counts for the transcript."""
    parser = MAP_REPO_TO_PARSER.get(repo, _parse_pytest)
    status = parser(extract_test_output(log))
    f2p_ok = sum(1 for t in fail_to_pass if status.get(t) == "PASSED")
    p2p_ok = sum(1 for t in pass_to_pass if status.get(t) == "PASSED")
    resolved = f2p_ok == len(fail_to_pass) and p2p_ok == len(pass_to_pass) and len(fail_to_pass) > 0
    return {
        "resolved": resolved,
        "fail_to_pass": [f2p_ok, len(fail_to_pass)],
        "pass_to_pass": [p2p_ok, len(pass_to_pass)],
        "n_parsed": len(status),
    }
