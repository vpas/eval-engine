"""IFEval instruction-following verifiers (a focused port of google-research/IFEval).

IFEval scores a response against a list of *programmatic* instructions ("write at least 3
paragraphs", "include the word 'however' twice", "wrap the answer in double quotes", …). The
upstream registry has ~25 instruction families; this is a faithful subset of the common,
dependency-free ones (no nltk/langdetect). The benchmark *converter* (``tools/fetch_benchmark.py``)
filters the sampled IFEval prompts down to those whose every ``instruction_id_list`` entry is
covered here — so our subset score is honest (every constraint is actually checked), just narrower
than the full benchmark.

Each verifier takes ``(response: str, **kwargs)`` and returns ``bool`` (satisfied). ``kwargs`` are the
per-instruction parameters IFEval ships in the dataset (e.g. ``{"num_words": 100, "relation": "at least"}``).
Headline metric = *strict prompt-level accuracy*: a sample passes iff **all** its instructions pass.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- helpers


def _compare(value: int, relation: str, target: int) -> bool:
    """IFEval relation comparator. ``relation`` ∈ {'at least','less than','at most','exactly'}."""
    if relation in ("at least", "at_least"):
        return value >= target
    if relation in ("less than", "less_than"):
        return value < target
    if relation in ("at most", "at_most"):
        return value <= target
    return value == target  # 'exactly' / unknown → exact


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def _sentence_count(text: str) -> int:
    # Approximate nltk sentence tokenization: split on sentence-final punctuation runs.
    parts = [s for s in re.split(r"[.!?]+(?:\s|$)", text.strip()) if s.strip()]
    return len(parts)


def _paragraphs(text: str) -> list[str]:
    # IFEval delimits paragraphs by markdown-style dividers: blank lines or '***'.
    blocks = re.split(r"\n\s*\*\s*\*\s*\*\s*\n|\n{2,}", text.strip())
    return [b for b in blocks if b.strip()]


# --------------------------------------------------------------------------- verifiers
# Each returns whether the response satisfies the instruction.


def _keywords_existence(response, keywords=None, **_):
    lo = response.lower()
    return all(k.lower() in lo for k in (keywords or []))


def _keywords_forbidden(response, forbidden_words=None, **_):
    lo = response.lower()
    return all(re.search(rf"\b{re.escape(w.lower())}\b", lo) is None for w in (forbidden_words or []))


def _keywords_frequency(response, keyword=None, frequency=None, relation="at least", **_):
    if keyword is None or frequency is None:
        return False
    n = len(re.findall(rf"\b{re.escape(keyword.lower())}\b", response.lower()))
    return _compare(n, relation, frequency)


def _keywords_letter_frequency(response, letter=None, let_frequency=None, let_relation="at least", **_):
    if letter is None or let_frequency is None:
        return False
    n = response.lower().count(letter.lower())
    return _compare(n, let_relation, let_frequency)


def _length_words(response, num_words=None, relation="at least", **_):
    if num_words is None:
        return False
    return _compare(_word_count(response), relation, num_words)


def _length_sentences(response, num_sentences=None, relation="at least", **_):
    if num_sentences is None:
        return False
    return _compare(_sentence_count(response), relation, num_sentences)


def _length_paragraphs(response, num_paragraphs=None, **_):
    if num_paragraphs is None:
        return False
    return len(_paragraphs(response)) == num_paragraphs


def _nth_paragraph_first_word(response, num_paragraphs=None, nth_paragraph=None, first_word=None, **_):
    if not (num_paragraphs and nth_paragraph and first_word):
        return False
    paras = _paragraphs(response)
    if len(paras) != num_paragraphs or nth_paragraph > len(paras):
        return False
    para = paras[nth_paragraph - 1].lstrip()
    m = re.match(r"\b(\w+)\b", para)
    return bool(m) and m.group(1).lower() == first_word.lower()


def _num_placeholders(response, num_placeholders=None, **_):
    if num_placeholders is None:
        return False
    return len(re.findall(r"\[.*?\]", response)) >= num_placeholders


def _postscript(response, postscript_marker="P.S.", **_):
    return postscript_marker.lower().replace(" ", "") in response.lower().replace(" ", "")


def _num_bullets(response, num_bullets=None, **_):
    if num_bullets is None:
        return False
    bullets = re.findall(r"^\s*[\*\-]\s+\S", response, re.MULTILINE)
    return len(bullets) == num_bullets


def _num_highlights(response, num_highlights=None, **_):
    if num_highlights is None:
        return False
    # *highlighted* or **highlighted** sections.
    highlights = re.findall(r"\*[^\*\n]+\*", response)
    return len(highlights) >= num_highlights


def _multiple_sections(response, section_spliter=None, num_sections=None, **_):
    if not (section_spliter and num_sections):
        return False
    n = len(re.findall(rf"{re.escape(section_spliter)}\s*\d+", response))
    return n >= num_sections


def _json_format(response, **_):
    import json
    text = response.strip()
    # tolerate a fenced ```json block
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    try:
        json.loads(text)
        return True
    except Exception:
        return False


def _title(response, **_):
    return bool(re.search(r"<<[^\n<>]+>>", response))


def _end_checker(response, end_phrase=None, **_):
    if end_phrase is None:
        return False
    return response.strip().lower().rstrip(".").endswith(end_phrase.strip().lower().rstrip("."))


def _quotation(response, **_):
    s = response.strip()
    return len(s) >= 2 and s[0] == '"' and s[-1] == '"'


def _no_comma(response, **_):
    return "," not in response


def _all_lowercase(response, **_):
    letters = [c for c in response if c.isalpha()]
    return bool(letters) and all(c.islower() for c in letters)


def _all_uppercase(response, **_):
    letters = [c for c in response if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def _capital_word_frequency(response, capital_frequency=None, capital_relation="at least", **_):
    if capital_frequency is None:
        return False
    caps = len([w for w in re.findall(r"\b[A-Za-z]+\b", response) if w.isupper()])
    return _compare(caps, capital_relation, capital_frequency)


def _repeat_prompt(response, prompt_to_repeat=None, **_):
    if not prompt_to_repeat:
        return False
    return response.strip().startswith(prompt_to_repeat.strip())


# instruction_id (as it appears in the IFEval dataset) → verifier
VERIFIERS = {
    "keywords:existence": _keywords_existence,
    "keywords:forbidden_words": _keywords_forbidden,
    "keywords:frequency": _keywords_frequency,
    "keywords:letter_frequency": _keywords_letter_frequency,
    "length_constraints:number_words": _length_words,
    "length_constraints:number_sentences": _length_sentences,
    "length_constraints:number_paragraphs": _length_paragraphs,
    "length_constraints:nth_paragraph_first_word": _nth_paragraph_first_word,
    "detectable_content:number_placeholders": _num_placeholders,
    "detectable_content:postscript": _postscript,
    "detectable_format:number_bullet_lists": _num_bullets,
    "detectable_format:number_highlighted_sections": _num_highlights,
    "detectable_format:multiple_sections": _multiple_sections,
    "detectable_format:json_format": _json_format,
    "detectable_format:title": _title,
    "startend:end_checker": _end_checker,
    "startend:quotation": _quotation,
    "punctuation:no_comma": _no_comma,
    "change_case:english_lowercase": _all_lowercase,
    "change_case:english_capital": _all_uppercase,
    "change_case:capital_word_frequency": _capital_word_frequency,
    "combination:repeat_prompt": _repeat_prompt,
}

SUPPORTED = set(VERIFIERS)


def evaluate(response: str, instruction_ids: list[str], kwargs_list: list[dict] | None) -> tuple[int, int]:
    """Return (satisfied, total) over a response's instructions. Unknown ids count as unsatisfied
    (the converter ensures our subset only contains supported ids, so this stays exact)."""
    kwargs_list = kwargs_list or [{}] * len(instruction_ids)
    satisfied = 0
    for iid, kw in zip(instruction_ids, kwargs_list):
        fn = VERIFIERS.get(iid)
        if fn and fn(response, **{k: v for k, v in (kw or {}).items() if v is not None}):
            satisfied += 1
    return satisfied, len(instruction_ids)
