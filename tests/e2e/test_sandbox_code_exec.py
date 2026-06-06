"""Validate the code-execution eval shape: the `code_generation` harness declares a sandbox and the
`code_exec` scorer runs the model's code against the sample's unit tests INSIDE it — the HumanEval
path (docs/ADDING_REAL_EVALS.md). Runs through the same spine as every other run.

Local Docker stands in for the production k8s sandbox (same Inspect `sandbox()` contract). Determinism
without a provider key: the mock model emits a fixed completion — a CORRECT solution passes the tests,
a WRONG one fails — proving the scorer actually executes + grades code, not just that the run finishes.

Requires docker + the python:3.11-slim image (as the agentic sandbox test).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from eval_engine import runner
from eval_engine.db import analytics, control
from eval_engine.models import PluginRef, RunSpec

# A self-contained HumanEval-style sample: a stub, a unit-test harness, and the entry point.
_SAMPLE = {
    "id": "ce1", "target": "", "metadata": {
        "category": "humaneval",
        "prompt": "def add(a, b):\n",
        "entry_point": "add",
        "test": "def check(candidate):\n    assert candidate(2, 3) == 5\n    assert candidate(0, 0) == 0\n",
    },
    "input": "Implement add(a, b).",
}


def _spec(mock_output: str) -> RunSpec:
    tmp = Path(tempfile.mkdtemp()) / "ce.jsonl"
    tmp.write_text(json.dumps(_SAMPLE) + "\n")
    return RunSpec(
        eval="code_exec_test", dataset=str(tmp), model="mockllm/model", batch_size=1, limit=1,
        mock_output=mock_output,
        harness=PluginRef(type="code_generation", config={"tools": []}),
        scorers=[PluginRef(type="code_exec", config={"timeout": 30})],
    )


def test_code_exec_passes_correct_and_fails_wrong():
    # CORRECT solution → tests pass → analytics records a pass.
    run_ok = runner.run(_spec("```python\ndef add(a, b):\n    return a + b\n```"))
    assert control.ledger_size(run_ok) == 0, "ledger not pruned after finalize"
    n, passed, *_ = analytics.run_summary(run_ok)
    assert n == 1 and passed == 1, f"correct code did not pass in the sandbox (n={n} passed={passed})"

    # WRONG solution → assertion fails inside the sandbox → not a pass (but still a clean run).
    run_bad = runner.run(_spec("```python\ndef add(a, b):\n    return a - b\n```"))
    n2, passed2, *_ = analytics.run_summary(run_bad)
    assert n2 == 1 and passed2 == 0, f"wrong code should fail the tests (n={n2} passed={passed2})"

    print(f"  [code-exec-sandbox] correct→pass ({run_ok}), wrong→fail ({run_bad}); "
          f"code executed + graded INSIDE the sandbox ✓")
