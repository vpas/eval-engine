"""eval-engine prototype CLI:  run | report | runs | catalog | ledger."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from . import builtins, plugins, runner  # noqa: F401  populate registry
from .db import analytics, control


def cmd_run(args):
    with open(args.spec) as fh:
        spec = runner.RunSpec(**yaml.safe_load(fh))
    print(
        f"▶ running '{spec.eval}' | model={spec.model} | harness={spec.harness.type} "
        f"| scorers={[s.type for s in spec.scorers]} | batch_size={spec.batch_size}"
    )
    run_id = runner.run(spec)
    print(f"✓ run {run_id} complete | ledger rows remaining for run: {control.ledger_size(run_id)} (pruned)")
    _print_report(run_id)


def cmd_report(args):
    _print_report(args.run_id)


def cmd_runs(args):
    rows = control.list_runs()
    if not rows:
        print("  (no runs yet)")
        return
    for rid, ev, model, acc, total, created in rows:
        acc_s = f"{acc:.2f}" if acc is not None else "  - "
        print(f"  {rid}  {ev:<16} {model:<18} acc={acc_s} n={total}  {created}")


def cmd_catalog(args):
    for p in plugins.catalog():
        print(f"  {p['kind']:<8} {p['name']}@{p['version']:<8} {p['description']}")


def cmd_ledger(args):
    print(f"  live ledger rows (all runs): {control.ledger_size()}")
    print("  (ephemeral — populated during a run, pruned at finalize; ORCHESTRATION §10)")


def _print_report(run_id: str):
    run = control.get_run(run_id)
    if not run:
        print(f"  no run {run_id}")
        return
    n, passed, mean, tokens, cost = analytics.run_summary(run_id)
    print(f"\n  run {run_id} | eval={run[1]} model={run[3]} | dataset_hash={run[13]}")
    print(f"  samples={n}  passed={passed}  accuracy={(passed or 0) / (n or 1):.0%}  "
          f"tokens={tokens}  cost=${cost:.6f}\n")

    print(f"  {'sample':<8} {'pass':<5} {'category':<12} {'score':<6} output → target")
    print(f"  {'-'*8} {'-'*4} {'-'*11} {'-'*5} {'-'*24}")
    for sid, p, gk, score, uri in analytics.samples(run_id):
        out = tgt = ""
        if uri and Path(uri).exists():
            t = json.loads(Path(uri).read_text())
            out, tgt = str(t.get("output", ""))[:18], str(t.get("target", ""))[:14]
        print(f"  {sid:<8} {'✓' if p else '✗':<5} {(gk or '-'):<12} {score:<6.2f} {out} → {tgt}")

    print(f"\n  accuracy by category (analytics slice):")
    for gk, c, pas, acc in analytics.by_category(run_id):
        print(f"    {gk or '(none)':<14} n={c} passed={pas} acc={acc}")
    print()


def main():
    ap = argparse.ArgumentParser(prog="eval-engine")
    sub = ap.add_subparsers(required=True)

    r = sub.add_parser("run", help="run an eval from a RunSpec yaml")
    r.add_argument("spec")
    r.set_defaults(func=cmd_run)

    rp = sub.add_parser("report", help="show a run's results")
    rp.add_argument("run_id")
    rp.set_defaults(func=cmd_report)

    sub.add_parser("runs", help="list runs").set_defaults(func=cmd_runs)
    sub.add_parser("catalog", help="list registered plugins").set_defaults(func=cmd_catalog)
    sub.add_parser("ledger", help="show live ephemeral ledger size").set_defaults(func=cmd_ledger)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
