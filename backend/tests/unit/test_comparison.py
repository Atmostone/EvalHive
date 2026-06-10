"""Unit tests for the Pairwise Comparison Framework pure helpers (E-21).

No DB, no LLM. Covers the position-bias reconciliation (agree → winner, disagree →
tie + flag), the comparisons → E-19 matches projection (verdict → outcome,
self/incomplete/no-verdict skipped, judge vs human source), the side-by-side judge
context shape, the prompt label, and judge↔human agreement.
"""

from types import SimpleNamespace

from app.quality.comparison import (
    _prompt_label,
    _reconcile,
    build_pair_context,
    comparisons_to_matches,
    judge_agreement,
)


def _task(title="Sort a list", description="do it", summary="", files=None):
    return SimpleNamespace(
        title=title,
        description=description,
        result_summary=summary,
        result_files=files or [],
    )


def _comp(player_a, player_b, *, judge=None, human=None):
    return SimpleNamespace(player_a=player_a, player_b=player_b, judge_verdict=judge, human_verdict=human)


# --------------------------------------------------------------------------- #
# Position-bias reconciliation
# --------------------------------------------------------------------------- #
def test_reconcile_agree_on_winner():
    assert _reconcile("a", "a") == ("a", False)
    assert _reconcile("b", "b") == ("b", False)


def test_reconcile_agree_on_tie():
    assert _reconcile("tie", "tie") == ("tie", False)


def test_reconcile_disagreement_is_tie_with_bias():
    # Any disagreement between the two orders → tie + position bias detected.
    assert _reconcile("a", "b") == ("tie", True)
    assert _reconcile("a", "tie") == ("tie", True)
    assert _reconcile("tie", "b") == ("tie", True)


# --------------------------------------------------------------------------- #
# comparisons → E-19 matches
# --------------------------------------------------------------------------- #
def test_matches_from_judge_verdicts():
    comps = [
        _comp("gpt", "claude", judge="a"),
        _comp("gpt", "claude", judge="tie"),
    ]
    matches = comparisons_to_matches(comps, source="judge")
    assert matches == [
        {"player_a": "gpt", "player_b": "claude", "outcome": "a", "weight": 1},
        {"player_a": "gpt", "player_b": "claude", "outcome": "tie", "weight": 1},
    ]


def test_matches_skip_self_incomplete_and_unverdicted():
    comps = [
        _comp("gpt", "gpt", judge="a"),  # self-match → dropped (E-19 would drop it)
        _comp("gpt", None, judge="a"),  # incomplete player → dropped
        _comp("gpt", "claude", judge=None),  # no judge verdict → dropped
    ]
    assert comparisons_to_matches(comps, source="judge") == []


def test_matches_source_selects_judge_or_human():
    comps = [_comp("gpt", "claude", judge="a", human="b")]
    assert comparisons_to_matches(comps, source="judge")[0]["outcome"] == "a"
    assert comparisons_to_matches(comps, source="human")[0]["outcome"] == "b"


# --------------------------------------------------------------------------- #
# Judge context
# --------------------------------------------------------------------------- #
def test_build_pair_context_has_both_answers_and_task():
    a = _task(summary="answer alpha", files=["a.py"])
    b = _task(summary="answer beta")
    ctx = build_pair_context(a, b)
    assert "=== Answer A ===" in ctx
    assert "=== Answer B ===" in ctx
    assert "answer alpha" in ctx and "answer beta" in ctx
    assert "Task: Sort a list" in ctx
    assert "Files: a.py" in ctx
    # The A/B order matches the argument order (what the position swap relies on).
    assert ctx.index("=== Answer A ===") < ctx.index("=== Answer B ===")


def test_build_pair_context_includes_reference_when_given():
    ctx = build_pair_context(_task(summary="x"), _task(summary="y"), reference="the gold answer")
    assert "Reference answer: the gold answer" in ctx


def test_build_pair_context_caps_long_answers():
    big = "z" * 50000
    ctx = build_pair_context(_task(summary=big), _task(summary="y"))
    assert "[truncated]" in ctx
    assert len(ctx) < 20000


# --------------------------------------------------------------------------- #
# Prompt label + agreement
# --------------------------------------------------------------------------- #
def test_prompt_label_is_stable_and_fingerprinted():
    a = _prompt_label("You are a careful coder.\nFollow the rules.")
    b = _prompt_label("You are a careful coder.\nFollow the rules.")
    c = _prompt_label("You are a sloppy coder.")
    assert a == b  # deterministic
    assert a != c  # different prompts → different labels
    assert "·" in a  # first line · fingerprint


def test_prompt_label_empty():
    assert _prompt_label("") == ""
    assert _prompt_label("   \n  ") == ""


def test_judge_agreement():
    comps = [
        _comp("g", "c", judge="a", human="a"),  # agree
        _comp("g", "c", judge="a", human="b"),  # disagree
        _comp("g", "c", judge="tie", human="tie"),  # agree
        _comp("g", "c", judge="a", human=None),  # only judge → excluded
    ]
    out = judge_agreement(comps)
    assert out == {"n": 3, "agreements": 2, "agreement": round(2 / 3, 3)}


def test_judge_agreement_empty():
    assert judge_agreement([]) == {"n": 0, "agreements": 0, "agreement": None}
