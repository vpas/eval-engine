"""eval-engine prototype CLI:  run | report | runs | catalog | ledger."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from . import builtins, db, plugins, runner  # noqa: F401  populate registry
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
    for r in rows:
        acc_s = f"{r['accuracy']:.2f}" if r["accuracy"] is not None else "  - "
        print(f"  {r['id']}  {r['eval_id']:<16} {r['model']:<18} "
              f"acc={acc_s} n={r['total']}  {r['created_at']}")


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
    s = analytics.run_summary(run_id)
    print(f"\n  run {run_id} | eval={run['eval_id']} model={run['model']} "
          f"| dataset_hash={run['dataset_hash']}")
    print(f"  samples={s.samples}  passed={s.passed}  "
          f"accuracy={(s.passed or 0) / (s.samples or 1):.0%}  "
          f"tokens={s.tokens}  cost=${s.cost:.6f}\n")

    print(f"  {'sample':<8} {'pass':<5} {'category':<12} {'score':<6} output → target")
    print(f"  {'-'*8} {'-'*4} {'-'*11} {'-'*5} {'-'*24}")
    for row in analytics.samples(run_id):
        out = tgt = ""
        if row.transcript_uri and Path(row.transcript_uri).exists():
            t = json.loads(Path(row.transcript_uri).read_text())
            out, tgt = str(t.get("output", ""))[:18], str(t.get("target", ""))[:14]
        print(f"  {row.sample_id:<8} {'✓' if row.passed else '✗':<5} {(row.group_key or '-'):<12} "
              f"{row.primary_score:<6.2f} {out} → {tgt}")

    print("\n  accuracy by category (analytics slice):")
    for c in analytics.by_category(run_id):
        print(f"    {c.group_key or '(none)':<14} n={c.n} passed={c.passed} acc={c.accuracy}")
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
    # ensure schema for DB-touching commands (no-op if already inited at import). `catalog` is a
    # pure in-memory registry op → must work with no database (handy as a container healthcheck).
    if args.func is not cmd_catalog:
        db.init()
    args.func(args)


if __name__ == "__main__":
    main()
