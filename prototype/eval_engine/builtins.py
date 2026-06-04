"""First-party harnesses & scorers — the prototype catalog seed (PLUGINS §9.3).

Importing this module populates the registry. Harnesses wrap Inspect solvers; scorers wrap
Inspect scorers. Pure A: we register/configure, we do not re-abstract Inspect's types.
"""
from __future__ import annotations

from pydantic import BaseModel

from inspect_ai.scorer import Scorer, includes, match, model_graded_qa
from inspect_ai.solver import Solver, generate

from .plugins import harness, scorer

# --------------------------------------------------------------------------- harnesses


class SingleTurnConfig(BaseModel):
    pass


@harness("single_turn", "1.0.0", SingleTurnConfig, description="One-shot generate; no tools.")
def single_turn(cfg: SingleTurnConfig) -> Solver:
    return generate()


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
