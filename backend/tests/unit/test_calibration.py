"""Unit tests for Confidence Calibration (E-16).

These cover the pure metric math (ECE / Brier / reliability diagram and the
per-model recommendation), the confidence clamp, and the self-probe LLM call's
parsing / budget / error handling (`_probe_confidence`, with the LLM mocked).
No network, no real DB.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.quality import calibration as cal
from app.quality.calibration import (
    _build_probe_input,
    _calibration_metrics,
    _clamp_confidence,
    _probe_confidence,
    _recommendation_for,
)


def _llm():
    return SimpleNamespace(
        model=SimpleNamespace(
            api_name="test-model", input_price_per_1m_usd=1, output_price_per_1m_usd=2
        ),
        provider=SimpleNamespace(api_key="k", endpoint="http://e/v1"),
    )


def _resp(confidence=0.8, reasoning="seems right", pt=120, ct=20):
    fn = MagicMock()
    fn.arguments = json.dumps({"confidence": confidence, "reasoning": reasoning})
    tc = MagicMock()
    tc.function = fn
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(tool_calls=[tc]))]
    resp.usage = {"prompt_tokens": pt, "completion_tokens": ct}
    return resp


class _FakeProvider:
    def __init__(self, **resp_kw):
        self._kw = resp_kw

    async def acompletion(self, **kw):
        return _resp(**self._kw)


class _BoomProvider:
    async def acompletion(self, **kw):
        raise RuntimeError("api down")


class _BadJsonProvider:
    async def acompletion(self, **kw):
        fn = MagicMock()
        fn.arguments = "{not valid json"
        tc = MagicMock()
        tc.function = fn
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(tool_calls=[tc]))]
        resp.usage = {}
        return resp


def _task(result="An answer.", title="T", description="D"):
    return SimpleNamespace(
        id="task-1", title=title, description=description, result_summary=result
    )


def _trace(steps=None):
    return {
        "task": {"id": "t", "title": "T", "description": "D"},
        "steps": steps
        if steps is not None
        else [
            {"seq": 0, "kind": "tool", "tool_name": "web_fetch",
             "content": "did the work", "truncated": False},
        ],
        "stats": {"original_tokens": 100, "cleaned_tokens": 50, "steps_total": 1},
    }


# --- confidence clamp --------------------------------------------------------


def test_clamp_confidence():
    assert _clamp_confidence(0.5) == 0.5
    assert _clamp_confidence(1.7) == 1.0
    assert _clamp_confidence(-0.3) == 0.0
    assert _clamp_confidence("bad") == 0.0
    assert _clamp_confidence(None) == 0.0
    assert _clamp_confidence(0.123456) == 0.123


# --- metric math -------------------------------------------------------------


def test_metrics_empty():
    m = _calibration_metrics([], bins=10)
    assert m["count"] == 0
    assert m["ece"] is None and m["brier"] is None
    assert len(m["reliability"]) == 10


def test_metrics_perfect_calibration():
    # In each used bucket, mean confidence == accuracy → ECE ≈ 0.
    pairs = [
        (0.0, False), (0.0, False),
        (1.0, True), (1.0, True),
        (0.5, True), (0.5, False),  # bucket [0.5,0.6): conf .5, acc .5
    ]
    m = _calibration_metrics(pairs, bins=10)
    assert m["count"] == 6
    assert m["ece"] == 0.0
    assert m["accuracy"] == 0.5
    assert m["avg_confidence"] == 0.5
    assert m["overconfidence"] == 0.0


def test_metrics_overconfidence_positive():
    # High stated confidence, low actual accuracy → overconfident, ECE > 0.
    pairs = [(0.9, False), (0.9, False), (0.9, False), (0.9, True)]
    m = _calibration_metrics(pairs, bins=10)
    assert m["overconfidence"] > 0
    assert m["ece"] > 0
    # Brier matches the manual mean of (conf-actual)^2.
    expected = round((0.81 + 0.81 + 0.81 + 0.01) / 4, 4)
    assert m["brier"] == expected


def test_metrics_brier_manual():
    pairs = [(0.8, False)]  # (0.8 - 0)^2 = 0.64
    m = _calibration_metrics(pairs, bins=10)
    assert m["brier"] == 0.64
    assert m["accuracy"] == 0.0
    assert m["avg_confidence"] == 0.8


def test_metrics_reliability_buckets():
    pairs = [(0.05, False), (0.95, True), (0.95, True)]
    m = _calibration_metrics(pairs, bins=10)
    rel = m["reliability"]
    # 0.05 lands in bucket 0 [0.0,0.1); 0.95 lands in bucket 9 [0.9,1.0).
    assert rel[0]["count"] == 1 and rel[0]["accuracy"] == 0.0
    assert rel[9]["count"] == 2 and rel[9]["accuracy"] == 1.0
    assert rel[9]["avg_confidence"] == 0.95
    # boundary value 1.0 must not overflow the last bucket
    m2 = _calibration_metrics([(1.0, True)], bins=10)
    assert m2["reliability"][9]["count"] == 1


# --- recommendation ----------------------------------------------------------


def test_recommendation_overestimates():
    metrics = _calibration_metrics(
        [(0.9, False), (0.9, False), (0.9, True)], bins=10
    )
    rec = _recommendation_for("glm-5.1", metrics, min_count=3)
    assert rec is not None
    assert "overestimates" in rec and "glm-5.1" in rec


def test_recommendation_underestimates():
    metrics = _calibration_metrics(
        [(0.1, True), (0.1, True), (0.1, False)], bins=10
    )
    rec = _recommendation_for("glm-5.1", metrics, min_count=3)
    assert rec is not None and "underestimates" in rec


def test_recommendation_insufficient_data():
    metrics = _calibration_metrics([(0.9, False)], bins=10)
    assert _recommendation_for("m", metrics, min_count=3) is None


# --- probe input -------------------------------------------------------------


def test_build_probe_input_omits_verdict_and_caps():
    text, capped = _build_probe_input(_task(result="my deliverable"), _trace(), 10_000)
    assert "my deliverable" in text
    assert "YOUR ANSWER" in text
    # The grader's verdict must never leak into the probe input.
    assert "weighted_score" not in text and "verdict" not in text.lower()
    assert capped is False


def test_build_probe_input_empty_trace():
    text, capped = _build_probe_input(_task(), _trace(steps=[]), 10_000)
    assert "(no recorded trajectory steps)" in text
    assert capped is False


# --- self-probe LLM call -----------------------------------------------------


async def test_probe_parses_and_clamps(monkeypatch):
    monkeypatch.setattr(cal, "get_llm_provider", lambda: _FakeProvider(confidence=1.4))
    out = await _probe_confidence(_task(), _trace(), _llm(), 10_000)
    assert out["status"] == "scored"
    assert out["confidence"] == 1.0  # clamped from 1.4
    assert out["judge_input_tokens"] == 120 and out["judge_output_tokens"] == 20
    assert out["judge_cost_usd"] > 0
    assert out["input_capped"] is False


async def test_probe_reasoning_truncated(monkeypatch):
    monkeypatch.setattr(
        cal, "get_llm_provider", lambda: _FakeProvider(reasoning="x" * 1000)
    )
    out = await _probe_confidence(_task(), _trace(), _llm(), 10_000)
    assert len(out["reasoning"]) == cal._REASON_CAP


async def test_probe_llm_failure_is_error(monkeypatch):
    monkeypatch.setattr(cal, "get_llm_provider", lambda: _BoomProvider())
    out = await _probe_confidence(_task(), _trace(), _llm(), 10_000)
    assert out["status"] == "error" and "api down" in out["error"]


async def test_probe_bad_json_is_error(monkeypatch):
    monkeypatch.setattr(cal, "get_llm_provider", lambda: _BadJsonProvider())
    out = await _probe_confidence(_task(), _trace(), _llm(), 10_000)
    assert out["status"] == "error"


async def test_probe_input_capped(monkeypatch):
    monkeypatch.setattr(cal, "get_llm_provider", lambda: _FakeProvider())
    big = [
        {"seq": i, "kind": "tool", "tool_name": "t", "content": "word " * 200,
         "truncated": False}
        for i in range(20)
    ]
    out = await _probe_confidence(_task(), _trace(big), _llm(), 200)
    assert out["status"] == "scored" and out["input_capped"] is True
