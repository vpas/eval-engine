#!/usr/bin/env python3
"""Register the benchmark suite as engine entities (docs/ADDING_REAL_EVALS.md §G6).

POSTs to the control-plane API so each benchmark is launchable from the dashboard's "from registered
eval" drawer: registers a `DatasetSpec` per subset (server-side snapshot + content-hash), a shared
cheap `ModelSpec`, and an `EvalSpec` (dataset + default harness + default scorers) per benchmark.

    # against a local stack (infra/up.sh + uvicorn) or a port-forward to the cluster API:
    python tools/seed_benchmarks.py --api http://localhost:8080

The harness/scorer config here mirrors examples/benchmarks/*.yaml (the CLI path) — same eval, two
front doors. Re-running mints new immutable entity versions (no in-place mutation, per §13/§14).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BENCH = "examples/benchmarks"

# A cheap, on-brand default target routed through the LiteLLM gateway → OpenRouter. Swap model_id for
# openai/gpt-4o-mini (or meta-llama/llama-3.1-8b-instruct) for an even cheaper floor.
MODEL = {
    "id": "demo-cheap", "provider": "openrouter", "model_id": "anthropic/claude-3.5-haiku",
    "description": "Cheap default target for the benchmark demo suite (< $10 total).",
}

# eval id → (dataset file stem, default harness PluginRef, [default scorer PluginRefs]).
SUITE = {
    "gpqa_diamond": ("gpqa",
                     {"type": "multiple_choice", "config": {"cot": True}}, [{"type": "choice"}]),
    "mmlu_subjects": ("mmlu",
                      {"type": "multiple_choice"}, [{"type": "choice"}]),
    "gsm8k": ("gsm8k",
              {"type": "single_turn",
               "config": {"prompt_suffix": "Reason step by step, then end with a line: "
                                           "'ANSWER: <number>'."}}, [{"type": "numeric_answer"}]),
    "math_500": ("math500",
                 {"type": "single_turn",
                  "config": {"prompt_suffix": "Put your final answer in \\boxed{}."}},
                 [{"type": "math"}]),
    "humaneval": ("humaneval",
                  {"type": "code_generation", "config": {"sandbox": "SANDBOX"}},  # filled from --sandbox
                  [{"type": "code_exec", "config": {"timeout": 30}}]),
    "ifeval": ("ifeval",
               {"type": "single_turn"}, [{"type": "ifeval"}]),
    "swe_bench_lite": ("swebench",
                       {"type": "swe_bench", "config": {"message_limit": 40}},
                       [{"type": "swe_bench"}]),
    # Alignment AUDIT, not an accuracy benchmark (docs/PETRI.md): the auditor + judge are baked into the
    # eval's default harness/scorer; the TARGET is the model the launcher picks. Polarity is inverted
    # (HIGH = concerning). Needs the [petri] extra in the image (Python >=3.12). Use `openrouter/<id>`
    # (NOT `openai/<id>`): the worker routes openrouter/ via Inspect's OpenRouter provider →
    # chat-completions → the LiteLLM gateway, avoiding the OpenAI Responses-API path that inspect's
    # native openai/ provider forces and that LiteLLM 405s on (docs/PETRI.md). Registering this makes
    # "petri_audit" appear in the dashboard launch drawer — pick an openrouter/ target + knobs and go.
    "petri_audit": ("petri_seeds",
                    {"type": "petri", "config": {
                        "auditor_model": "openrouter/anthropic/claude-sonnet-4.5",
                        "judge_model": "openrouter/anthropic/claude-sonnet-4.5",
                        "max_turns": 15}},
                    [{"type": "petri_judge", "config": {"flag_threshold": 5}}]),
}


def _post(api: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(api.rstrip("/") + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", default="http://localhost:8080", help="control-plane API base URL")
    ap.add_argument("--dataset-uri-prefix", default=str(REPO),
                    help="prefix the API reads dataset files from (default: this repo root). For the "
                         "cluster pass /app (where the image holds examples/benchmarks/).")
    ap.add_argument("--sandbox", default="docker", choices=["docker", "k8s"],
                    help="sandbox for the code_generation (humaneval) harness: docker (local) | k8s "
                         "(in-cluster per-sample pods).")
    args = ap.parse_args()

    # fill the humaneval harness sandbox from --sandbox
    for _eid, (_stem, harness, _scorers) in SUITE.items():
        if harness.get("config", {}).get("sandbox") == "SANDBOX":
            harness["config"]["sandbox"] = args.sandbox

    def uri(stem: str) -> str:
        return f"{args.dataset_uri_prefix.rstrip('/')}/{BENCH}/{stem}.jsonl"

    try:
        print(f"model: {_post(args.api, '/models', MODEL)}")
        for eval_id, (stem, harness, scorers) in SUITE.items():
            ds_id = f"{stem}_subset"
            print(f"dataset: {_post(args.api, '/datasets', {'id': ds_id, 'source': 'jsonl', 'uri': uri(stem), 'description': f'{eval_id} benchmark subset'})}")
            ev = {"id": eval_id, "dataset": ds_id, "default_harness": harness,
                  "default_scorers": scorers, "description": f"{eval_id} (benchmark subset)"}
            print(f"eval: {_post(args.api, '/evals', ev)}")
    except urllib.error.URLError as e:
        sys.exit(f"✗ could not reach API at {args.api}: {e}. Is the control plane up "
                 f"(infra/up.sh + uvicorn, or a port-forward to the cluster)?")
    print("✓ seeded. Launch from the dashboard's 'from registered eval' drawer, or via "
          "POST /evals/<id>/launch.")


if __name__ == "__main__":
    main()
