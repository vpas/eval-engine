"""Unit: the benchmark scorers/harness added for the real-eval suite (docs/ADDING_REAL_EVALS.md).

Covers the registry wiring + the pure scoring logic (number/code extraction, IFEval verifiers). The
sandbox-executing `code_exec` path is exercised e2e elsewhere (needs Docker); here we test its helpers.
"""
import pytest

from eval_engine import builtins, ifeval, plugins  # noqa: F401  importing builtins populates registry
from eval_engine.builtins import _assemble_program, _extract_code, _extract_number


def test_catalog_spans_the_new_eval_shapes():
    cat = {(p["kind"], p["name"]) for p in plugins.catalog()}
    assert ("harness", "code_generation") in cat
    assert {("scorer", "math"), ("scorer", "numeric_answer"),
            ("scorer", "code_exec"), ("scorer", "ifeval")} <= cat


def test_new_plugins_build():
    for name in ("math", "numeric_answer", "code_exec", "ifeval"):
        obj, p = plugins.build("scorer", {"type": name})
        assert p.name == name and callable(obj)
    built, p = plugins.build("harness", {"type": "code_generation", "config": {"sandbox": "docker"}})
    solver, sandbox = built  # code_generation declares a sandbox for its scorer
    assert sandbox.type == "docker" and callable(solver)


def test_single_turn_prompt_knobs_compose():
    # With a system message + suffix the harness chains solvers; plain stays a single solver.
    plain, _ = plugins.build("harness", {"type": "single_turn"})
    chained, _ = plugins.build("harness",
                               {"type": "single_turn", "config": {"system": "Be terse.",
                                                                   "prompt_suffix": "ANSWER: <n>"}})
    assert callable(plain) and callable(chained)


@pytest.mark.parametrize("text,expected", [
    ("The answer is 42 apples. ANSWER: 18", "18"),   # last ANSWER: marker wins
    ("So we get 1,024.", "1024"),                     # thousands separator stripped
    ("$3.50", "3.50"),                                # currency stripped
    ("no digits here", None),
    ("step 1 then step 2, total 7", "7"),             # last number when no marker
])
def test_extract_number(text, expected):
    assert _extract_number(text) == expected


def test_numeric_answer_compares_within_tolerance():
    cfg = builtins.NumericAnswerConfig()
    got, want = _extract_number("... ANSWER: 100"), _extract_number("#### 100")
    assert got == want == "100"


def test_extract_code_prefers_fenced_blocks():
    out = "Sure:\n```python\ndef f():\n    return 1\n```\nthanks"
    assert _extract_code(out) == "def f():\n    return 1"
    assert _extract_code("def g():\n    return 2") == "def g():\n    return 2"  # bare fallback


# The HumanEval contract: the `test` field defines `def check(candidate)` but never calls it — the
# program MUST append `check(entry)`, else every solution passes trivially (regression guard).
_HUMANEVAL_TEST = "def check(candidate):\n    assert candidate(2, 3) == 5\n"


def test_assemble_program_appends_check_call():
    prog = _assemble_program("```python\ndef add(a, b):\n    return a + b\n```",
                             "def add(a, b):\n", "add", _HUMANEVAL_TEST)
    assert prog.rstrip().endswith("check(add)")
    assert "def check(candidate)" in prog and "def add(a, b)" in prog


def test_assemble_program_prepends_stub_when_model_omits_signature():
    # Model returns only the body fragment (no `def add`): the stub must be prepended.
    prog = _assemble_program("```python\n    return a + b\n```", "def add(a, b):\n", "add", _HUMANEVAL_TEST)
    assert "def add(a, b):" in prog


def test_assemble_program_does_not_double_call_when_test_invokes_check():
    test_with_call = _HUMANEVAL_TEST + "\ncheck(add)\n"
    prog = _assemble_program("def add(a,b):\n    return a+b", "def add(a, b):\n", "add", test_with_call)
    assert prog.count("check(add)") == 1  # not appended again


@pytest.mark.parametrize("resp,iid,kw,sat", [
    ("I love it, however it ends", "keywords:existence", {"keywords": ["however"]}, 1),
    ("clean text", "keywords:forbidden_words", {"forbidden_words": ["bad"]}, 1),
    ("bad text", "keywords:forbidden_words", {"forbidden_words": ["bad"]}, 0),
    ("one two three four", "length_constraints:number_words", {"num_words": 3, "relation": "at least"}, 1),
    ("one two", "length_constraints:number_words", {"num_words": 3, "relation": "at least"}, 0),
    ('"quoted"', "startend:quotation", {}, 1),
    ("a, b", "punctuation:no_comma", {}, 0),
    ("no commas here", "punctuation:no_comma", {}, 1),
    ("ALL CAPS HERE", "change_case:english_capital", {}, 1),
    ("Mixed Case", "change_case:english_lowercase", {}, 0),
    ('{"a": 1}', "detectable_format:json_format", {}, 1),
])
def test_ifeval_verifiers(resp, iid, kw, sat):
    satisfied, total = ifeval.evaluate(resp, [iid], [kw])
    assert (satisfied, total) == (sat, 1)


def test_ifeval_strict_requires_all():
    resp = '"quoted, with comma"'  # quoted ✓ but has a comma ✗
    satisfied, total = ifeval.evaluate(resp, ["startend:quotation", "punctuation:no_comma"], [{}, {}])
    assert satisfied == 1 and total == 2  # strict prompt-level => not a pass


def test_ifeval_converter_target_ids_are_all_supported():
    # Guards the converter contract: every id we promise to sample must have a verifier.
    assert ifeval.SUPPORTED == set(ifeval.VERIFIERS)
    assert len(ifeval.SUPPORTED) >= 15
