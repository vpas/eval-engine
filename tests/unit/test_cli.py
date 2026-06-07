"""Unit: CLI rendering — the `runs` / `report` commands format the data layer's rows correctly.

These guard against column-count / column-order drift: the CLI reads rows from control + analytics,
and when those tables grew columns the CLI's positional unpacking silently went wrong (a crash on
`runs`, and `report` mislabeled cost as dataset_hash). The data layer now returns dict/named rows, so
the CLI reads by name; these tests pin that the commands run and surface the right fields. No backends
— control/analytics are monkeypatched to return correctly-shaped rows.
"""
from argparse import Namespace

from eval_engine import analytics, cli, control


def _fake_runs():
    return [
        {"id": "abc123", "eval_id": "gsm8k", "eval_version": 1, "model": "mockllm/model",
         "accuracy": 0.75, "total": 4, "cost_usd": 0.0, "created_at": "2026-06-07T00:00:00",
         "created_by": "me", "status": "completed", "sweep": None},
    ]


def _fake_run():
    return {"id": "abc123", "eval_id": "gsm8k", "eval_version": 1, "model": "mockllm/model",
            "provider": "mockllm", "model_id": "model", "harness": "single_turn", "scorers": [],
            "status": "completed", "total": 4, "done": 4, "failed": 0, "accuracy": 0.75,
            "cost_usd": 0.0, "dataset_hash": "deadbeefcafe0001", "created_by": "me", "team": None,
            "image_digest": "dev", "lane": "interactive", "created_at": "2026-06-07T00:00:00",
            "finished_at": "2026-06-07T00:01:00", "provider_fingerprint": "mockllm/model"}


def test_cmd_runs_renders_each_run(monkeypatch, capsys):
    monkeypatch.setattr(control, "list_runs", _fake_runs)
    cli.cmd_runs(Namespace())                       # would raise on the old positional unpack
    out = capsys.readouterr().out
    assert "abc123" in out and "gsm8k" in out and "acc=0.75" in out and "n=4" in out


def test_cmd_runs_handles_no_runs(monkeypatch, capsys):
    monkeypatch.setattr(control, "list_runs", list)
    cli.cmd_runs(Namespace())
    assert "no runs yet" in capsys.readouterr().out


def test_cmd_report_uses_named_fields(monkeypatch, capsys):
    monkeypatch.setattr(control, "get_run", lambda rid: _fake_run())
    monkeypatch.setattr(analytics, "run_summary",
                        lambda rid: analytics.RunSummary(4, 3, 0.75, 1234, 0.0))
    monkeypatch.setattr(analytics, "samples", lambda rid: [
        analytics.SampleRow("s1", 1, "math", 1.0, "", 100, 0, ""),
        analytics.SampleRow("s2", 0, "math", 0.0, "", 90, 0, ""),
    ])
    monkeypatch.setattr(analytics, "by_category",
                        lambda rid: [analytics.CategoryRow("math", 2, 1, 0.5)])
    cli.cmd_report(Namespace(run_id="abc123"))
    out = capsys.readouterr().out
    # the report header shows the real dataset_hash (the old positional code printed cost here)
    assert "dataset_hash=deadbeefcafe0001" in out
    assert "samples=4" in out and "passed=3" in out
    assert "s1" in out and "s2" in out and "math" in out


def test_cmd_report_unknown_run(monkeypatch, capsys):
    monkeypatch.setattr(control, "get_run", lambda rid: None)
    cli.cmd_report(Namespace(run_id="nope"))
    assert "no run nope" in capsys.readouterr().out
