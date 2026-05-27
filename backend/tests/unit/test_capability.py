"""Unit tests for the Capability-isolation harness (E-13, part A).

Covers spec normalization, the Glass-Box tool_used check (all / any), the
four-cell classification, and outcome-correctness derivation from an E-02
profile (weighted score vs the preferred reference dimension). The DB-backed
evaluate_task_capability / aggregate_capability are exercised in the integration
tests.
"""

from app.quality.capability import (
    CHEATED,
    DEFAULT_MATCH,
    FAILED_NO_TOOL,
    FAILED_WITH_TOOL,
    GENUINE,
    _outcome_from_profile,
    classify,
    normalize_spec,
    tool_used,
)


# --- normalize_spec -------------------------------------------------------


def test_normalize_spec_full():
    spec = normalize_spec(
        {"required_tools": ["web_search", "fetch_url"], "category": "fresh_data", "match": "any"}
    )
    assert spec == {
        "required_tools": ["web_search", "fetch_url"],
        "category": "fresh_data",
        "match": "any",
    }


def test_normalize_spec_defaults_match_and_strips():
    spec = normalize_spec({"required_tools": [" bash ", "", "  "]})
    assert spec["required_tools"] == ["bash"]
    assert spec["match"] == DEFAULT_MATCH
    assert spec["category"] is None


def test_normalize_spec_invalid_returns_none():
    for bad in [None, 42, "tools", {}, {"required_tools": []}, {"required_tools": "bash"}, {"category": "x"}]:
        assert normalize_spec(bad) is None


def test_normalize_spec_bad_match_falls_back():
    spec = normalize_spec({"required_tools": ["a"], "match": "most"})
    assert spec["match"] == DEFAULT_MATCH


# --- tool_used (Glass-Box) ------------------------------------------------


def test_tool_used_all_subset_present():
    used, missing = tool_used(["a", "b"], ["a", "b", "c"], "all")
    assert used is True and missing == []


def test_tool_used_all_one_missing():
    used, missing = tool_used(["a", "b"], ["a", "c"], "all")
    assert used is False and missing == ["b"]


def test_tool_used_any_one_present():
    used, missing = tool_used(["a", "b"], ["b", "c"], "any")
    assert used is True and missing == ["a"]


def test_tool_used_any_none_present():
    used, missing = tool_used(["a", "b"], ["c", "d"], "any")
    assert used is False and missing == ["a", "b"]


def test_tool_used_case_insensitive():
    used, missing = tool_used(["Web_Search"], ["web_search"], "all")
    assert used is True and missing == []


def test_tool_used_matches_mcp_prefixed_name():
    # the agent exposes MCP tools as <server>__<tool>; a bare required name matches
    used, missing = tool_used(["web_search", "now"], ["web__web_search", "time__now"], "all")
    assert used is True and missing == []


# --- classify -------------------------------------------------------------


def test_classify_matrix():
    assert classify(True, True) == GENUINE
    assert classify(True, False) == CHEATED
    assert classify(False, True) == FAILED_WITH_TOOL
    assert classify(False, False) == FAILED_NO_TOOL


# --- _outcome_from_profile ------------------------------------------------


def test_outcome_weighted_above_threshold():
    correct, signal, score = _outcome_from_profile({"weighted_score": 8.0}, 7.0)
    assert correct is True and signal == "judge" and score == 8.0


def test_outcome_weighted_below_threshold():
    correct, signal, score = _outcome_from_profile({"weighted_score": 5.0}, 7.0)
    assert correct is False and signal == "judge" and score == 5.0


def test_outcome_reference_takes_priority():
    # A scored reference dimension wins over a (low) weighted score.
    profile = {
        "weighted_score": 2.0,
        "dimensions": [
            {"evaluator": "judge", "status": "scored", "score": 2},
            {"evaluator": "reference", "status": "scored", "score": 10, "threshold": 6, "passed": True},
        ],
    }
    correct, signal, score = _outcome_from_profile(profile, 7.0)
    assert correct is True and signal == "reference" and score == 10.0


def test_outcome_reference_without_threshold_uses_score():
    profile = {
        "dimensions": [
            {"evaluator": "reference", "status": "scored", "score": 9},
        ],
    }
    correct, signal, score = _outcome_from_profile(profile, 7.0)
    assert correct is True and signal == "reference" and score == 9.0


def test_outcome_none_when_no_signal():
    assert _outcome_from_profile(None, 7.0) == (False, "none", None)
    assert _outcome_from_profile({}, 7.0) == (False, "none", None)
