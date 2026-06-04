"""Validate the (a) leap: an AGENTIC harness whose tool calls execute inside a sandbox, run
through the SAME spine (ledger → claim → Inspect → commit → analytics → finalize → prune).

Local Docker stands in for the production Kubernetes sandbox provider (docs/SANDBOXING.md) — the
Inspect `sandbox()` contract is identical; only the provider changes (like SQLite→Postgres
elsewhere). The sandbox here is hardened + AIR-GAPPED (sandbox/airgap-compose.yaml): network_mode
none, read-only rootfs, non-root, dropped caps, pid/mem caps (§5 baseline hardening, §2 air-gap).

Determinism without a provider key: a scripted mock agent (RunSpec.mock_tool_calls) emits
[bash printenv EE_SANDBOX_SECRET] → [submit]. The PROOF that the tool ran *inside the container*
is the bash tool's OUTPUT carrying the secret — a value set ONLY in the compose, which the worker
process does not have. (The mock's submit is hardcoded, so the score alone proves nothing; the
tool output does.)

Requires docker + the pre-pulled python:3.11-slim image:
  PYTHONPATH=. ../.venv/bin/python tests/test_sandbox_agentic.py
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from inspect_ai.log import list_eval_logs, read_eval_log

from eval_engine import runner
from eval_engine.db import analytics, control
from eval_engine.models import PluginRef, RunSpec

SECRET = "in-sandbox-7f3a9c"  # must match environment.EE_SANDBOX_SECRET in airgap-compose.yaml


def _spec() -> RunSpec:
    tmp = Path(tempfile.mkdtemp()) / "sb.jsonl"
    # NB: the secret value is deliberately NOT in the prompt — only the compose sets it.
    tmp.write_text(json.dumps({
        "id": "sb1", "target": SECRET, "metadata": {"category": "agentic"},
        "input": "Read EE_SANDBOX_SECRET with the bash tool and submit it.",
    }) + "\n")
    return RunSpec(
        eval="agentic_sandbox_test", dataset=str(tmp), model="mockllm/model",
        batch_size=1, limit=1,
        harness=PluginRef(type="agentic", config={"tools": ["bash"]}),
        scorers=[PluginRef(type="includes", config={"ignore_case": True})],
        mock_tool_calls=[
            {"tool": "bash", "args": {"cmd": "printenv EE_SANDBOX_SECRET"}},
            {"tool": "submit", "args": {"answer": SECRET}},
        ],
    )


def _bash_output_from_newest_log() -> str:
    """The tool output Inspect captured — our proof the bash ran inside the sandbox container."""
    logs = list_eval_logs(str(control.DATA / "logs"))
    newest = max(logs, key=lambda li: li.mtime or "")
    log = read_eval_log(newest)
    outputs = []
    for s in log.samples or []:
        for m in s.messages:
            if type(m).__name__ == "ChatMessageTool":
                outputs.append(m.text or "")
    return "\n".join(outputs)


def test_agentic_sandbox():
    assert os.environ.get("EE_SANDBOX_SECRET") is None, \
        "worker process must NOT have the secret (else the proof is meaningless)"

    spec = _spec()
    run_id = runner.run(spec)  # full spine, single-process, sqlite backend

    # 1) the spine ran the agentic harness to completion
    assert control.ledger_size(run_id) == 0, "ledger not pruned after finalize"
    n, passed, *_ = analytics.run_summary(run_id)
    assert n == 1, f"analytics has {n} rows != 1"

    # 2) PROOF: the bash tool executed INSIDE the air-gapped container and read the secret that
    #    exists only there (the worker, asserted above, does not have it).
    tool_output = _bash_output_from_newest_log()
    assert SECRET in tool_output, \
        f"secret not in bash tool output {tool_output!r} — tool did not run in the sandbox"

    print(f"  [agentic-sandbox] run {run_id} | agentic harness through the spine ✓ | "
          f"bash ran INSIDE the air-gapped container (read {SECRET!r} the worker lacks) ✓ | "
          f"analytics rows={n} passed={passed} ledger pruned ✓")


if __name__ == "__main__":
    print("Agentic harness + air-gapped Docker sandbox through the spine:")
    test_agentic_sandbox()
    print("\nALL PASS ✓")
