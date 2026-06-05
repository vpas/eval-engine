"""First-party harnesses & scorers — the prototype catalog seed (PLUGINS §9.3).

Importing this module populates the registry. Harnesses wrap Inspect solvers; scorers wrap
Inspect scorers. Pure A: we register/configure, we do not re-abstract Inspect's types.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from inspect_ai.scorer import Scorer, choice, includes, match, model_graded_qa
from inspect_ai.solver import Solver, basic_agent, generate, multiple_choice
from inspect_ai.tool import bash, python
from inspect_ai.util import SandboxEnvironmentSpec

from .plugins import harness, scorer

PROTOTYPE_ROOT = Path(__file__).resolve().parent.parent  # for resolving sandbox compose paths

# --------------------------------------------------------------------------- harnesses


class SingleTurnConfig(BaseModel):
    pass


@harness("single_turn", "1.0.0", SingleTurnConfig, description="One-shot generate; no tools.")
def single_turn(cfg: SingleTurnConfig) -> Solver:
    return generate()


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
    only ``sandbox: docker|k8s`` changes — like SQLite→Postgres."""
    factories = {"bash": lambda: bash(timeout=cfg.tool_timeout),
                 "python": lambda: python(timeout=cfg.tool_timeout)}
    tools = [factories[t]() for t in cfg.tools]
    solver = basic_agent(tools=tools, message_limit=cfg.message_limit)

    if cfg.sandbox == "k8s":
        # In-cluster: inspect-k8s-sandbox helm-installs an ephemeral pod per sample (SANDBOXING.md).
        # Needs helm + RBAC on the worker; a values file tunes the chart to this cluster's runtime.
        import k8s_sandbox  # noqa: F401  registers the "k8s" sandbox provider with Inspect
        values = Path(cfg.k8s_values) if cfg.k8s_values else None
        if values and not values.is_absolute():
            values = PROTOTYPE_ROOT / values
        return solver, (SandboxEnvironmentSpec("k8s", str(values))
                        if values and values.exists() else SandboxEnvironmentSpec("k8s"))

    compose = Path(cfg.compose_file)
    if not compose.is_absolute():
        compose = PROTOTYPE_ROOT / compose
    if not compose.exists():
        raise FileNotFoundError(f"sandbox compose file not found: {compose}")
    return solver, SandboxEnvironmentSpec("docker", str(compose))


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
