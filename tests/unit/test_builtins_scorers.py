"""Unit: the async ``score()`` bodies of the dependency-free built-in scorers — pure, no backends.

The extraction helpers (``_extract_number``, ``_extract_code``, ``_assemble_program``,
``_extract_mc_letter``, the ifeval verifiers) are covered in test_benchmark_plugins; here we exercise
the *scorer wrappers themselves* — the CORRECT/INCORRECT decision + the answer/explanation they emit —
which the helper tests don't reach. Only the sandbox-free scorers are unit-testable: ``code_exec`` and
``swe_bench`` (with a script) run code in a sandbox and are covered by the e2e layer; ``swe_bench`` with
no ``eval_script`` short-circuits before the sandbox, so its guard is unit-testable here.

The scorer factories return the bare ``async score(state, target)`` (the plugin decorator returns the
function unchanged), so we call them directly with lightweight stand-ins for TaskState/Target.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from inspect_ai.scorer import CORRECT, INCORRECT

from eval_engine import builtins as b


def _state(completion="", metadata=None):
    return SimpleNamespace(output=SimpleNamespace(completion=completion), metadata=metadata or {})


def _target(text=""):
    return SimpleNamespace(text=text)


def _score(scorer_fn, completion="", target_text="", metadata=None):
    return asyncio.run(scorer_fn(_state(completion, metadata), _target(target_text)))


# --------------------------------------------------------------------------- numeric_answer

def _numeric(tolerance=1e-6):
    return b.numeric_answer(b.NumericAnswerConfig(tolerance=tolerance))


def test_numeric_exact_match_is_correct():
    s = _score(_numeric(), completion="ANSWER: 42", target_text="42")
    assert s.value == CORRECT and s.answer == "42"


def test_numeric_answer_marker_wins_over_earlier_numbers():
    # "answer is 7" then "ANSWER: 42" — the last marker is the model's final answer.
    s = _score(_numeric(), completion="I first thought answer is 7, but ANSWER: 42", target_text="42")
    assert s.value == CORRECT and s.answer == "42"


def test_numeric_falls_back_to_last_number_without_a_marker():
    s = _score(_numeric(), completion="the total comes to 5, then 9", target_text="9")
    assert s.value == CORRECT


def test_numeric_within_tolerance_is_correct_outside_is_not():
    assert _score(_numeric(tolerance=0.01), completion="3.14", target_text="3.141").value == CORRECT
    assert _score(_numeric(tolerance=1e-6), completion="3.14", target_text="3.141").value == INCORRECT


def test_numeric_strips_currency_and_thousands_separators():
    s = _score(_numeric(), completion="It costs $1,234.50", target_text="1234.5")
    assert s.value == CORRECT


def test_numeric_no_number_in_output_is_incorrect():
    s = _score(_numeric(), completion="I'm not sure, sorry.", target_text="42")
    assert s.value == INCORRECT and s.answer == ""


# --------------------------------------------------------------------------- ifeval (strict, all-or-nothing)

def _ifeval():
    return b.ifeval(b.IFEvalConfig())

def test_ifeval_all_instructions_satisfied_is_correct():
    s = _score(_ifeval(), completion="hello world",
               metadata={"instruction_id_list": ["change_case:english_lowercase",
                                                  "punctuation:no_comma"]})
    assert s.value == CORRECT and "2/2" in s.explanation


def test_ifeval_is_strict_one_unsatisfied_fails_the_sample():
    # lowercase holds, but "english_capital" does not → 1/2 → strict prompt-level accuracy fails.
    s = _score(_ifeval(), completion="hello world",
               metadata={"instruction_id_list": ["change_case:english_lowercase",
                                                  "change_case:english_capital"]})
    assert s.value == INCORRECT and "1/2" in s.explanation


def test_ifeval_no_instructions_is_incorrect_not_a_vacuous_pass():
    # total==0 must NOT count as "all satisfied" — an empty constraint list scores 0, not 1.
    s = _score(_ifeval(), completion="anything", metadata={"instruction_id_list": []})
    assert s.value == INCORRECT


# --------------------------------------------------------------------------- swe_bench guard (pre-sandbox)

def test_swe_bench_without_eval_script_is_unresolved():
    # No eval_script in metadata → short-circuit to INCORRECT before touching a sandbox.
    s = _score(b.swe_bench_scorer(b.SWEBenchScorerConfig()), completion="", metadata={})
    assert s.value == INCORRECT and "no eval_script" in s.explanation
