"""First-party harnesses & scorers — the prototype catalog seed (PLUGINS §9.3).

Importing this module populates the registry. Harnesses wrap Inspect solvers; scorers wrap
Inspect scorers. Pure A: we register/configure, we do not re-abstract Inspect's types.
"""
from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from inspect_ai.scorer import (CORRECT, INCORRECT, Score, Scorer, Target, accuracy, choice,
                               includes, match, model_graded_qa, stderr)
from inspect_ai.scorer import math as inspect_math
from inspect_ai.scorer import scorer as inspect_scorer
from inspect_ai.solver import (Generate, Solver, TaskState, basic_agent, chain, generate,
                               multiple_choice, prompt_template, solver, system_message)
from inspect_ai.tool import bash, python
from inspect_ai.util import SandboxEnvironmentSpec, sandbox

from . import ifeval as _ifeval
from . import swebench as _swebench
from .plugins import harness, scorer

PROTOTYPE_ROOT = Path(__file__).resolve().parent.parent  # for resolving sandbox compose paths

# Default sandbox specs reused by the agentic + code_generation harnesses (docs/SANDBOXING.md):
# local Docker (hardened, air-gapped) by default, an in-cluster per-sample K8s pod when sandbox="k8s".
_DEFAULT_COMPOSE = "deploy/sandbox/airgap-compose.yaml"
_DEFAULT_K8S_VALUES = "deploy/sandbox/k8s-agent-env-values.yaml"


def _sandbox_spec(sandbox_kind: str, compose_file: str, k8s_values: str | None) -> SandboxEnvironmentSpec:
    """Resolve a harness's declared sandbox (docs/SANDBOXING §4/§7) — shared by agentic + code_generation."""
    if sandbox_kind == "k8s":
        import k8s_sandbox  # noqa: F401  registers the "k8s" sandbox provider with Inspect
        values = Path(k8s_values) if k8s_values else None
        if values and not values.is_absolute():
            values = PROTOTYPE_ROOT / values
        return (SandboxEnvironmentSpec("k8s", str(values))
                if values and values.exists() else SandboxEnvironmentSpec("k8s"))
    compose = Path(compose_file)
    if not compose.is_absolute():
        compose = PROTOTYPE_ROOT / compose
    if not compose.exists():
        raise FileNotFoundError(f"sandbox compose file not found: {compose}")
    return SandboxEnvironmentSpec("docker", str(compose))

# --------------------------------------------------------------------------- harnesses


class SingleTurnConfig(BaseModel):
    # Free-form evals (GSM8K / MATH) need the model to emit a *parseable* final answer for the scorer
    # to extract. These nudge it without touching the dataset: a system message and/or an instruction
    # appended to each prompt (e.g. "End with 'ANSWER: <number>'" or "Put your final answer in \boxed{}").
    system: str = ""
    prompt_suffix: str = ""


@harness("single_turn", "1.0.0", SingleTurnConfig,
         description="One-shot generate; no tools. Optional `system` message + `prompt_suffix` to "
                     "steer the model to a parseable final answer (for the math/numeric scorers).")
def single_turn(cfg: SingleTurnConfig) -> Solver:
    steps = []
    if cfg.system:
        steps.append(system_message(cfg.system))
    if cfg.prompt_suffix:
        steps.append(prompt_template("{prompt}\n\n" + cfg.prompt_suffix))
    steps.append(generate())
    return chain(steps) if len(steps) > 1 else steps[0]


class CodeGenerationConfig(BaseModel):
    """Code-gen harness for execution-scored benchmarks (HumanEval/MBPP). The *solver* is a plain
    one-shot generate, but the harness still declares a **sandbox** — because the `code_exec` scorer
    runs the model's code against the unit tests inside it (docs/PLUGINS.md: a harness may declare a
    sandbox for its scorer, not just for tool calls). Pair with the `code_exec` scorer."""
    sandbox: str = "docker"                                # "docker" (local) | "k8s" (cluster)
    compose_file: str = _DEFAULT_COMPOSE
    k8s_values: str | None = _DEFAULT_K8S_VALUES
    system: str = ""


@harness("code_generation", "1.0.0", CodeGenerationConfig,
         description="One-shot code generation in a sandbox; pair with the 'code_exec' scorer "
                     "(which runs the completion against the sample's unit tests in the sandbox).")
def code_generation(cfg: CodeGenerationConfig) -> tuple[Solver, SandboxEnvironmentSpec]:
    solver = chain([system_message(cfg.system), generate()]) if cfg.system else generate()
    return solver, _sandbox_spec(cfg.sandbox, cfg.compose_file, cfg.k8s_values)


_SWE_SYSTEM = """You are an expert software engineer fixing a real GitHub issue.

The repository is already checked out at /testbed (your working directory). Use the bash tool to \
explore the code, then EDIT the source files in /testbed to resolve the issue described below. Do not \
write new test files — a hidden test suite will judge your fix. When you believe the issue is fixed, \
submit. The Python environment is the conda env 'testbed' (activate with \
`source /opt/miniconda3/bin/activate testbed` if you want to run code)."""


class SWEBenchConfig(BaseModel):
    message_limit: int = 40   # agent turn cap (exploration + edits)
    tool_timeout: int = 120   # per bash call


@harness("swe_bench", "1.0.0", SWEBenchConfig,
         description="Agentic software-engineering (SWE-bench): a bash agent edits the repo at /testbed "
                     "inside the instance's official image; pair with the 'swe_bench' scorer. The "
                     "per-instance sandbox is set per-sample from the dataset (metadata.image).")
def swe_bench(cfg: SWEBenchConfig) -> Solver:
    # The sandbox is per-SAMPLE (each instance runs in its own official image — set on Sample.sandbox by
    # the dataset loader), so the harness returns ONLY a solver; the runner passes no task-level sandbox.
    agent = basic_agent(tools=[bash(timeout=cfg.tool_timeout)], message_limit=cfg.message_limit)
    return chain([system_message(_SWE_SYSTEM), agent])


class MultipleChoiceConfig(BaseModel):
    cot: bool = False  # let the model reason (chain-of-thought) before selecting


def _extract_mc_letter(text: str, n: int) -> str | None:
    """Best-effort extraction of the selected option letter when the model didn't emit a clean
    'ANSWER: X' line. Handles 'thinking' models that conclude with \\boxed{D}, 'the answer is D',
    '**D**', or a trailing bare letter. Returns an uppercase letter within A..A+n-1, or None."""
    valid = {chr(ord("A") + i) for i in range(n)}
    patterns = [
        r"\\boxed\{\s*\\?(?:text|mathrm)?\{?\s*([A-Za-z])\b",  # \boxed{D}, \boxed{\text{D}}
        r"(?i)\banswer\s*(?:is|:)\s*\(?\*{0,2}([A-Za-z])\b",   # "answer is D", "answer: D", "answer is **D**"
        r"\*\*\s*([A-Za-z])\s*\*\*",                            # **D**
        r"(?im)^\s*\(?([A-Za-z])\)?\s*[.:)]?\s*$",              # a line that is just the letter
    ]
    for pat in patterns:
        matches = re.findall(pat, text)
        for cand in reversed(matches):                          # last occurrence wins (final answer)
            if cand.upper() in valid:
                return cand.upper()
    return None


@solver
def _mc_answer_fallback() -> Solver:
    """Runs AFTER Inspect's multiple_choice solver. If its strict 'ANSWER:' parse marked no selection
    (common for reasoning models that answer with \\boxed{}), re-extract the letter from the completion
    and mark that choice — so a correctly-reasoned answer in a non-standard format still scores."""
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        if any(c.correct is True for c in state.choices):
            return state  # the solver already captured a selection — leave it
        letter = _extract_mc_letter(state.output.completion if state.output else "", len(state.choices))
        if letter:
            idx = ord(letter) - ord("A")
            for i in range(len(state.choices)):
                state.choices.mark_choice(i, i == idx)
        return state
    return solve


@harness("multiple_choice", "1.0.0", MultipleChoiceConfig,
         description="Multiple-choice: present lettered choices, model selects one (pair with the "
                     "'choice' scorer; dataset samples need a `choices` list + letter `target`). "
                     "Falls back to \\boxed{}/free-form answer extraction for reasoning models.")
def multiple_choice_harness(cfg: MultipleChoiceConfig) -> Solver:
    return chain([multiple_choice(cot=cfg.cot), _mc_answer_fallback()])


class AgenticConfig(BaseModel):
    tools: list[str] = ["bash"]                            # sandbox tools to expose: bash | python
    sandbox: str = "docker"                                # "docker" (local) | "k8s" (cluster)
    compose_file: str = "deploy/sandbox/airgap-compose.yaml"  # docker only: hardened AIR-GAPPED spec
    k8s_values: str | None = "deploy/sandbox/k8s-agent-env-values.yaml"  # k8s: Helm values (cluster-specific runtime)
    message_limit: int = 12                                # cap the agent loop
    tool_timeout: int = 30


@harness(
    "agentic",
    "1.0.0",
    AgenticConfig,
    description="Tool-use agent (bash/python) whose tool calls execute inside a sandbox.",
)
def agentic(cfg: AgenticConfig) -> tuple[Solver, SandboxEnvironmentSpec]:
    """Agentic harness — the (a) leap from single_turn. Returns BOTH a tool-use solver AND the
    sandbox the orchestrator must provision (docs/SANDBOXING §4/§7: "the harness declares its
    sandbox; the orchestrator provisions accordingly"). Tool calls run in the sandbox, model calls
    go worker→gateway (§2) — so the sandbox can be air-gapped. Locally that sandbox is Docker; in
    production it's a hardened, air-gapped per-sample K8s pod (docs/SANDBOXING.md), with a pooled
    sandbox service as the trigger-gated scale-up (docs/FUTURE.md §4). The contract is identical;
    only ``sandbox: docker|k8s`` changes — a config choice, not a code change."""
    factories = {"bash": lambda: bash(timeout=cfg.tool_timeout),
                 "python": lambda: python(timeout=cfg.tool_timeout)}
    tools = [factories[t]() for t in cfg.tools]
    solver = basic_agent(tools=tools, message_limit=cfg.message_limit)
    return solver, _sandbox_spec(cfg.sandbox, cfg.compose_file, cfg.k8s_values)


# --------------------------------------------------------------------------- scorers


def _simple_scorer(name: str, score_fn) -> Scorer:
    """Wrap an ``async score(state, target) -> Score`` in the accuracy()+stderr() metrics factory our
    custom scorers all share — so each scorer is just its scoring body, not the repeated
    inspect_scorer/_factory shell."""
    @inspect_scorer(metrics=[accuracy(), stderr()], name=name)
    def _factory() -> Scorer:
        return score_fn
    return _factory()


class IncludesConfig(BaseModel):
    ignore_case: bool = True


@scorer(
    "includes",
    "1.0.0",
    IncludesConfig,
    primary_metric="accuracy",
    description="Correct if target is a substring of the output.",
)
def includes_scorer(cfg: IncludesConfig) -> Scorer:
    return includes(ignore_case=cfg.ignore_case)


class MatchConfig(BaseModel):
    location: str = "end"  # 'begin' | 'end' | 'any' | 'exact'
    ignore_case: bool = True


@scorer(
    "match",
    "1.0.0",
    MatchConfig,
    primary_metric="accuracy",
    description="Correct if output matches target at the given location.",
)
def match_scorer(cfg: MatchConfig) -> Scorer:
    return match(location=cfg.location, ignore_case=cfg.ignore_case)


class ChoiceConfig(BaseModel):
    pass


@scorer(
    "choice",
    "1.0.0",
    ChoiceConfig,
    primary_metric="accuracy",
    description="Score a multiple_choice selection against the target letter(s).",
)
def choice_scorer(cfg: ChoiceConfig) -> Scorer:
    return choice()


class LLMJudgeConfig(BaseModel):
    model: str | None = None
    template: str | None = None


@scorer(
    "llm_judge",
    "1.0.0",
    LLMJudgeConfig,
    primary_metric="accuracy",
    description="Model-graded QA against a rubric/template (LLM-as-judge).",
)
def llm_judge(cfg: LLMJudgeConfig) -> Scorer:
    kwargs = {}
    if cfg.model:
        kwargs["model"] = cfg.model
    if cfg.template:
        kwargs["template"] = cfg.template
    return model_graded_qa(**kwargs)


# ---- math (MATH-500): symbolic equivalence via Inspect core's math() scorer (sympy under the hood).

class MathConfig(BaseModel):
    pass


@scorer(
    "math",
    "1.0.0",
    MathConfig,
    primary_metric="accuracy",
    description="Mathematical-equivalence answer checking (Inspect core `math()`; extracts the final "
                "answer — e.g. from \\boxed{} — and compares via sympy). For MATH/AIME-style evals.",
)
def math_scorer(cfg: MathConfig) -> Scorer:
    return inspect_math()


# ---- numeric_answer (GSM8K): dependency-free final-number extraction + numeric comparison.

class NumericAnswerConfig(BaseModel):
    tolerance: float = 1e-6


_NUM_RE = re.compile(r"-?\$?\d[\d,]*\.?\d*")


def _extract_number(text: str) -> str | None:
    """Pull the model's final numeric answer: prefer the value after an 'ANSWER:'/'answer is' marker,
    else the last number in the text. Strips $ and thousands separators."""
    text = text.strip()
    markers = re.findall(r"(?:ANSWER|answer)\s*(?:is|:)?\s*(-?\$?\d[\d,]*\.?\d*)", text)
    nums = [markers[-1]] if markers else _NUM_RE.findall(text)  # last 'ANSWER:' wins, else last number
    if not nums:
        return None
    return nums[-1].replace("$", "").replace(",", "").rstrip(".")


@scorer(
    "numeric_answer",
    "1.0.0",
    NumericAnswerConfig,
    primary_metric="accuracy",
    description="Extract the final number from the output (after 'ANSWER:' or the last number) and "
                "compare numerically to the target within a tolerance. For GSM8K-style numeric evals.",
)
def numeric_answer(cfg: NumericAnswerConfig) -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        out = state.output.completion if state.output else ""
        got = _extract_number(out)
        want = _extract_number(target.text)
        ok = False
        if got is not None and want is not None:
            try:
                ok = abs(float(got) - float(want)) <= cfg.tolerance
            except ValueError:
                ok = got == want
        return Score(value=CORRECT if ok else INCORRECT, answer=got or "",
                     explanation=f"extracted={got!r} target={want!r}")
    return _simple_scorer("numeric_answer", score)


# ---- code_exec (HumanEval/MBPP): run the model's code against the sample's unit tests IN THE SANDBOX.

class CodeExecConfig(BaseModel):
    timeout: int = 30  # per-sample wall-clock cap for running the tests in the sandbox


def _extract_code(completion: str) -> str:
    """Pull Python source from a chat completion: concatenate fenced ```python blocks if present,
    else fall back to the raw text (some models answer with bare code)."""
    blocks = re.findall(r"```(?:python)?\s*\n(.*?)```", completion, re.DOTALL)
    return "\n\n".join(b.strip("\n") for b in blocks) if blocks else completion.strip()


def _assemble_program(completion: str, stub: str, entry: str, test: str) -> str:
    """Build the executable test program (HumanEval contract): extracted code (+ the signature stub if
    the model didn't redefine the entry function) + the test harness + a top-level ``check(entry)``
    call. The test DEFINES ``def check(candidate)`` but doesn't invoke it, so we append the call unless
    the test already calls check() at top level (not the ``def`` line)."""
    code = _extract_code(completion)
    if entry and stub and f"def {entry}" not in code:
        code = stub + "\n" + code
    program = code + "\n\n" + test
    if entry and not re.search(r"(?m)^\s*check\s*\(", test):
        program += f"\n\ncheck({entry})\n"
    return program


@scorer(
    "code_exec",
    "1.0.0",
    CodeExecConfig,
    primary_metric="accuracy",
    description="Execute the model's generated code against the sample's unit tests inside the "
                "sandbox (pair with the 'code_generation' harness). Sample metadata supplies `test` "
                "(+ optional `prompt` stub and `entry_point`); pass iff the test program exits 0.",
)
def code_exec(cfg: CodeExecConfig) -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        md = state.metadata or {}
        completion = state.output.completion if state.output else ""
        test = md.get("test", "") or target.text
        program = _assemble_program(completion, md.get("prompt", ""),
                                    md.get("entry_point", ""), test)
        code = _extract_code(completion)
        try:
            result = await sandbox().exec(["python3", "-c", program], timeout=cfg.timeout)
            ok = result.success
            detail = (result.stderr or result.stdout or "")[-800:]
        except Exception as e:  # sandbox/timeout error → not correct (and surfaced for debugging)
            ok, detail = False, f"exec error: {e}"[:800]
        return Score(value=CORRECT if ok else INCORRECT, answer=code[:1000], explanation=detail)
    return _simple_scorer("code_exec", score)


# ---- ifeval (IFEval): programmatic instruction-following verifiers (see eval_engine/ifeval.py).

class IFEvalConfig(BaseModel):
    pass


@scorer(
    "ifeval",
    "1.0.0",
    IFEvalConfig,
    primary_metric="accuracy",
    description="Instruction-following (IFEval): check the response against the sample's programmatic "
                "constraints (metadata `instruction_id_list` + `kwargs`). Strict prompt-level accuracy "
                "— passes iff ALL instructions are satisfied.",
)
def ifeval(cfg: IFEvalConfig) -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        md = state.metadata or {}
        resp = state.output.completion if state.output else ""
        ids = md.get("instruction_id_list", []) or []
        satisfied, total = _ifeval.evaluate(resp, ids, md.get("kwargs"))
        ok = total > 0 and satisfied == total
        return Score(value=CORRECT if ok else INCORRECT, answer="",
                     explanation=f"{satisfied}/{total} instructions satisfied")
    return _simple_scorer("ifeval", score)


# ---- swe_bench (SWE-bench): apply the held-out test patch + run the repo's tests IN THE SANDBOX.

class SWEBenchScorerConfig(BaseModel):
    timeout: int = 1200  # the test suite can be slow; generous wall-clock cap for the eval script


@scorer(
    "swe_bench",
    "1.0.0",
    SWEBenchScorerConfig,
    primary_metric="accuracy",
    description="Score a SWE-bench instance: run the precomputed eval script (applies the held-out "
                "test patch + runs the repo's tests) in the instance sandbox, then resolve iff the "
                "FAIL_TO_PASS tests pass and PASS_TO_PASS tests still pass. Pair with 'swe_bench' harness.",
)
def swe_bench_scorer(cfg: SWEBenchScorerConfig) -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        md = state.metadata or {}
        eval_script = md.get("eval_script", "")
        if not eval_script:
            return Score(value=INCORRECT, explanation="no eval_script in sample metadata")
        try:
            # The agent has edited /testbed; the eval script applies the test patch + runs tests.
            # `exec 2>&1` merges stderr into stdout IN EXECUTION ORDER. The eval script prints its
            # `>>>>> Start/End Test Output` markers via `set -x` (→ stderr) while pytest's
            # PASSED/FAILED lines go to stdout; capturing the streams separately and concatenating
            # them would place the markers AFTER all results, so the slice between them would hold
            # no test outcomes (n_parsed=0 → every instance unresolved). Merging keeps them
            # interleaved, matching how the official SWE-bench harness captures combined output.
            result = await sandbox().exec(["bash", "-c", "exec 2>&1\n" + eval_script], timeout=cfg.timeout)
            log = result.stdout or result.stderr or ""
        except Exception as e:  # sandbox/timeout → unresolved (surfaced for debugging)
            return Score(value=INCORRECT, explanation=f"eval-script error: {e}"[:500])
        report = _swebench.grade(log, md.get("repo", ""),
                                 md.get("FAIL_TO_PASS", []), md.get("PASS_TO_PASS", []))
        f2p, p2p = report["fail_to_pass"], report["pass_to_pass"]
        return Score(
            value=CORRECT if report["resolved"] else INCORRECT,
            answer="resolved" if report["resolved"] else "unresolved",
            explanation=f"FAIL_TO_PASS {f2p[0]}/{f2p[1]}, PASS_TO_PASS {p2p[0]}/{p2p[1]} "
                        f"(parsed {report['n_parsed']} tests)",
        )
    return _simple_scorer("swe_bench", score)
