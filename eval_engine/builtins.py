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
from inspect_ai.solver import (Solver, TaskState, basic_agent, chain, generate, multiple_choice,
                               prompt_template, system_message)
from inspect_ai.tool import bash, python
from inspect_ai.util import SandboxEnvironmentSpec, sandbox

from . import ifeval as _ifeval
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


class MultipleChoiceConfig(BaseModel):
    cot: bool = False  # let the model reason (chain-of-thought) before selecting


@harness("multiple_choice", "1.0.0", MultipleChoiceConfig,
         description="Multiple-choice: present lettered choices, model selects one (pair with the "
                     "'choice' scorer; dataset samples need a `choices` list + letter `target`).")
def multiple_choice_harness(cfg: MultipleChoiceConfig) -> Solver:
    return multiple_choice(cot=cfg.cot)


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
    @inspect_scorer(metrics=[accuracy(), stderr()], name="numeric_answer")
    def _factory() -> Scorer:
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
        return score
    return _factory()


# ---- code_exec (HumanEval/MBPP): run the model's code against the sample's unit tests IN THE SANDBOX.

class CodeExecConfig(BaseModel):
    timeout: int = 30  # per-sample wall-clock cap for running the tests in the sandbox


def _extract_code(completion: str) -> str:
    """Pull Python source from a chat completion: concatenate fenced ```python blocks if present,
    else fall back to the raw text (some models answer with bare code)."""
    blocks = re.findall(r"```(?:python)?\s*\n(.*?)```", completion, re.DOTALL)
    return "\n\n".join(b.strip("\n") for b in blocks) if blocks else completion.strip()


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
    @inspect_scorer(metrics=[accuracy(), stderr()], name="code_exec")
    def _factory() -> Scorer:
        async def score(state: TaskState, target: Target) -> Score:
            md = state.metadata or {}
            completion = state.output.completion if state.output else ""
            code = _extract_code(completion)
            stub = md.get("prompt", "")
            entry = md.get("entry_point", "")
            test = md.get("test", "") or target.text
            # If the model didn't redefine the target function, prepend the signature stub.
            if entry and stub and f"def {entry}" not in code:
                code = stub + "\n" + code
            program = code + "\n\n" + test
            if entry and "check(" not in test:
                program += f"\n\ncheck({entry})\n"
            try:
                result = await sandbox().exec(["python3", "-c", program], timeout=cfg.timeout)
                ok = result.success
                detail = (result.stderr or result.stdout or "")[-800:]
            except Exception as e:  # sandbox/timeout error → not correct (and surfaced for debugging)
                ok, detail = False, f"exec error: {e}"[:800]
            return Score(value=CORRECT if ok else INCORRECT, answer=code[:1000], explanation=detail)
        return score
    return _factory()


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
    @inspect_scorer(metrics=[accuracy(), stderr()], name="ifeval")
    def _factory() -> Scorer:
        async def score(state: TaskState, target: Target) -> Score:
            md = state.metadata or {}
            resp = state.output.completion if state.output else ""
            ids = md.get("instruction_id_list", []) or []
            satisfied, total = _ifeval.evaluate(resp, ids, md.get("kwargs"))
            ok = total > 0 and satisfied == total
            return Score(value=CORRECT if ok else INCORRECT, answer="",
                         explanation=f"{satisfied}/{total} instructions satisfied")
        return score
    return _factory()
