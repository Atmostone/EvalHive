"""Unit tests for annotation shaping and the frozen judge observation (E-05)."""

from types import SimpleNamespace

from app.quality.feedback import (
    FEEDBACK_SCHEMA_VERSION,
    _band,
    build_human_feedback,
    freeze_judge_observation,
    observed_reasoning,
    observed_scores,
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
    fb = build_human_feedback(payload, observation=None, submitted_by="u@example.com")
    by = {d["key"]: d for d in fb["dimensions"]}
    assert by["a"]["score"] == 10 and by["a"]["band"] == "good"
    assert by["b"]["score"] == 0 and by["b"]["band"] == "bad"
    assert by["c"]["name"] == "c" and by["c"]["band"] == "improve"
    assert fb["schema_version"] == FEEDBACK_SCHEMA_VERSION
    assert fb["submitted_by"] == "u@example.com" and fb["submitted_at"]


def test_build_pairs_judge_score():
    observation = {"outcome": {"scores": {"a": 6, "b": 9}}}
    payload = {"dimensions": [{"key": "a", "name": "A", "score": 3}]}
    fb = build_human_feedback(payload, observation, "u@example.com")
    assert fb["dimensions"][0]["judge_score"] == 6
    # a key with no judge counterpart pairs with None
    fb2 = build_human_feedback(
        {"dimensions": [{"key": "z", "name": "Z", "score": 5}]},
        observation,
        "u@example.com",
    )
    assert fb2["dimensions"][0]["judge_score"] is None


def test_build_pairs_trajectory_axes():
    """A trajectory-axis rating must carry the E-07 judge's score.

    Pairing only the outcome dimensions left every process axis at judge_score
    None, so the calibration pair had to be rebuilt from a live, mutable profile
    — which is what let a re-judge move a past κ (SPA-85)."""
    observation = {
        "outcome": {"scores": {"correctness": 6}},
        "trajectory": {"scores": {"efficiency": 8}},
    }
    fb = build_human_feedback(
        {
            "dimensions": [
                {"key": "correctness", "name": "Correctness", "score": 5},
                {"key": "efficiency", "name": "Efficiency", "score": 7},
            ]
        },
        observation,
        "u@example.com",
    )
    by = {d["key"]: d for d in fb["dimensions"]}
    assert by["correctness"]["judge_score"] == 6
    assert by["efficiency"]["judge_score"] == 8


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


# --------------------------------------------------------------------------- #
# freeze_judge_observation
# --------------------------------------------------------------------------- #
def _record(**kw):
    base = {"quality_profile": None, "trajectory_profile": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_freeze_captures_both_judges():
    obs = freeze_judge_observation(
        _record(
            quality_profile={
                "judge_model": "j-1",
                "rubric_id": "r-1",
                "rubric_name": "R",
                "schema_version": 3,
                "evaluated_at": "2026-08-01T00:00:00",
                "gate": {"passed": True},
                "dimensions": [
                    {"key": "correctness", "score": 7, "reasoning": "because"},
                ],
            },
            trajectory_profile={
                "judge_model": "j-1",
                "schema_version": 2,
                "evaluated_at": "2026-08-01T00:01:00",
                "axes": [{"key": "efficiency", "score": 4, "reason": "wandered"}],
            },
        )
    )
    assert obs["outcome"]["judge_model"] == "j-1"
    assert obs["outcome"]["rubric_name"] == "R"
    assert obs["outcome"]["gate_passed"] is True
    assert obs["outcome"]["scores"] == {"correctness": 7}
    assert obs["trajectory"]["scores"] == {"efficiency": 4}
    # Flattened views used by the calibration collector.
    assert observed_scores(obs) == {"correctness": 7, "efficiency": 4}
    assert observed_reasoning(obs) == {"correctness": "because", "efficiency": "wandered"}


def test_freeze_of_unjudged_record_is_empty():
    assert freeze_judge_observation(_record()) == {}
    assert observed_scores(None) == {}
    assert observed_reasoning({}) == {}
