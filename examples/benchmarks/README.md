# Benchmark subsets

Small, **deterministic** subsets of real benchmarks — the kind Anthropic reports in Claude model cards —
used to demo Eval Engine across different eval *shapes* under a < $10 compute budget. See
`docs/ADDING_REAL_EVALS.md` for the design.

These JSONLs are **generated**, not hand-authored: regenerate with

```bash
pip install datasets                 # tooling only — the engine never imports `datasets`
python tools/fetch_benchmark.py      # all, or e.g.: python tools/fetch_benchmark.py gsm8k mmlu
```

The subset is pinned by a fixed RNG seed (`tools/fetch_benchmark.py:SEED`), so the same items are
selected every run. Register them via `POST /datasets` (or `tools/seed_benchmarks.py`) to
content-address + snapshot them; runs then execute against the pinned snapshot with no HF access.

| File | Benchmark | Shape | Harness × scorer | n | Source (HF) | License |
|---|---|---|---|---|---|---|
| `gpqa.jsonl` | GPQA Diamond | hard MCQ | `multiple_choice` × `choice` | 50 | `fingertap/GPQA-diamond` (open Diamond mirror) | CC-BY-4.0 |
| `mmlu.jsonl` | MMLU (4 subjects) | MCQ, per-category | `multiple_choice` × `choice` | 60 | `cais/mmlu` | MIT |
| `gsm8k.jsonl` | GSM8K | free-form numeric | `single_turn` × `numeric_answer` | 50 | `openai/gsm8k` | MIT |
| `math500.jsonl` | MATH-500 | symbolic math | `single_turn` × `math` | 40 | `HuggingFaceH4/MATH-500` | MIT |
| `humaneval.jsonl` | HumanEval | code + sandbox exec | `code_generation` × `code_exec` | 40 | `openai/openai_humaneval` | MIT |
| `ifeval.jsonl` | IFEval | instruction-following | `single_turn` × `ifeval` | 50 | `google/IFEval` | Apache-2.0 |

`metadata.category` is the ClickHouse `group_key` (the per-category dashboard breakdown). For MMLU it's
the subject; for MATH-500 it's the math area.

**Notes**
- **GPQA**: the canonical `Idavidrein/gpqa` is gated; we use an open Diamond mirror and parse its inline
  options into a `choices` list + letter target (`tools/fetch_benchmark.py:_parse_gpqa`).
- **IFEval**: narrowed to prompts whose every instruction has a verifier in `eval_engine/ifeval.py`
  (so every constraint is actually checked) — a faithful subset, slightly narrower than the full set.
- **HumanEval**: `metadata` carries `prompt` (stub), `test` (unit tests), and `entry_point`; the
  `code_exec` scorer runs the model's code against the tests **inside the sandbox** (needs Docker/k8s).
