"""First-party harnesses & scorers — the prototype catalog seed (PLUGINS §9.3).

Importing this module populates the registry. Harnesses wrap Inspect solvers; scorers wrap
Inspect scorers. Pure A: we register/configure, we do not re-abstract Inspect's types.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from inspect_ai.scorer import Scorer, includes, match, model_graded_qa
from inspect_ai.solver import Solver, basic_agent, generate
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


class AgenticConfig(BaseModel):
    tools: list[str] = ["bash"]                            # sandbox tools to expose: bash | python
    sandbox: str = "docker"                                # provider; production swaps to "k8s"
    compose_file: str = "deploy/sandbox/airgap-compose.yaml"  # the hardened, AIR-GAPPED sandbox spec
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

    compose = Path(cfg.compose_file)
    if not compose.is_absolute():
        compose = PROTOTYPE_ROOT / compose
    if not compose.exists():
        raise FileNotFoundError(f"sandbox compose file not found: {compose}")
    return solver, SandboxEnvironmentSpec(cfg.sandbox, str(compose))


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
