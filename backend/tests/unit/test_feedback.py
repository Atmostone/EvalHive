"""Unit tests for human feedback shaping (E-05)."""

from app.quality.feedback import (
    FEEDBACK_SCHEMA_VERSION,
    _band,
    build_human_feedback,
)


def test_band_boundaries():
    assert _band(0) == "bad"
    assert _band(1) == "bad"
    assert _band(3) == "bad"
    assert _band(4) == "improve"
    assert _band(7) == "improve"
    assert _band(8) == "good"
    assert _band(10) == "good"


def test_build_clamps_and_bands():
    payload = {
        "dimensions": [
            {"key": "a", "name": "A", "score": 15},   # clamps to 10 → good
            {"key": "b", "name": "B", "score": -3},    # clamps to 0  → bad
            {"key": "c", "score": 5},                  # name falls back to key
        ]
    }
    fb = build_human_feedback(payload, profile=None, submitted_by="u@example.com")
    by = {d["key"]: d for d in fb["dimensions"]}
    assert by["a"]["score"] == 10 and by["a"]["band"] == "good"
    assert by["b"]["score"] == 0 and by["b"]["band"] == "bad"
    assert by["c"]["name"] == "c" and by["c"]["band"] == "improve"
    assert fb["schema_version"] == FEEDBACK_SCHEMA_VERSION
    assert fb["submitted_by"] == "u@example.com" and fb["submitted_at"]


def test_build_pairs_judge_score():
    profile = {"dimensions": [{"key": "a", "score": 6}, {"key": "b", "score": 9}]}
    payload = {"dimensions": [{"key": "a", "name": "A", "score": 3}]}
    fb = build_human_feedback(payload, profile, "u@example.com")
    assert fb["dimensions"][0]["judge_score"] == 6
    # a key with no judge counterpart pairs with None
    fb2 = build_human_feedback(
        {"dimensions": [{"key": "z", "name": "Z", "score": 5}]}, profile, "u@example.com"
    )
    assert fb2["dimensions"][0]["judge_score"] is None


def test_build_normalizes_verdict_and_comments():
    fb = build_human_feedback(
        {"verdict": "bogus", "overall_comment": "  ok  ",
         "dimensions": [{"key": "a", "name": "A", "score": 5, "comment": "  "}]},
        None, "u@example.com",
    )
    assert fb["verdict"] is None              # invalid verdict dropped
    assert fb["overall_comment"] == "ok"      # trimmed
    assert fb["dimensions"][0]["comment"] is None  # blank → None

    fb2 = build_human_feedback({"verdict": "approve", "dimensions": []}, None, "u@example.com")
    assert fb2["verdict"] == "approve"
    assert fb2["dimensions"] == [] and fb2["overall_comment"] is None
