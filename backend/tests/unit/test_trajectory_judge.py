"""Unit tests for the 6-axis Trajectory Judge (E-07).

The LLM call is mocked (a fake provider returns a canned score_trajectory tool
call), so these tests exercise prompt serialization, the input-budget cap, and
the parsing/clamping/error handling of `_judge_trajectory` without a network or
a DB.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.quality import trajectory as traj
from app.quality.trace_cleaner import _count_tokens
from app.quality.trajectory import (
    AXES,
    _fit_trace_to_budget,
    _judge_trajectory,
    _serialize_trace,
)


def _llm():
    return SimpleNamespace(
        model=SimpleNamespace(
            api_name="test-model", input_price_per_1m_usd=1, output_price_per_1m_usd=2
        ),
        provider=SimpleNamespace(api_key="k", endpoint="http://e/v1"),
    )


def _args(loop=10, eff=6):
    return {
        "efficiency": {"score": eff, "reason": "3 redundant searches"},
        "tool_selection": {"score": 9, "reason": "right tools"},
        "parameter_quality": {"score": 8, "reason": "params ok"},
        "error_recovery": {"score": 5, "reason": "repeated 404 twice"},
        "goal_alignment": {"score": 9, "reason": "moved toward goal"},
        "loop_detection": {"score": loop, "reason": "no cycles"},
        "summary": "goal reached, path suboptimal",
    }


def _resp(args, pt=120, ct=30):
    fn = MagicMock()
    fn.arguments = json.dumps(args)
    tc = MagicMock()
    tc.function = fn
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(tool_calls=[tc]))]
    resp.usage = {"prompt_tokens": pt, "completion_tokens": ct}
    return resp


class _FakeProvider:
    def __init__(self, args):
        self._args = args
        self.last_kwargs = None

    async def acompletion(self, **kw):
        self.last_kwargs = kw
        return _resp(self._args)


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


def _trace(steps=None):
    return {
        "task": {"id": "t", "title": "Build X", "description": "Desc"},
        "steps": steps
        if steps is not None
        else [
            {"seq": 0, "kind": "reasoning", "tool_name": None, "content": "plan it", "truncated": False},
            {"seq": 1, "kind": "tool", "tool_name": "web_search", "content": "results", "truncated": False},
        ],
        "stats": {"original_tokens": 100, "cleaned_tokens": 50, "steps_total": 2},
    }


# --- serialization / budget -----------------------------------------------


def test_serialize_contains_task_and_steps():
    text = _serialize_trace(_trace())
    assert "Build X" in text and "web_search" in text and "[0]" in text


def test_fit_within_budget_not_capped():
    text, capped = _fit_trace_to_budget(_trace(), 10_000)
    assert capped is False and "Build X" in text


def test_fit_over_budget_drops_middle_steps():
    big = [
        {"seq": i, "kind": "tool", "tool_name": "t", "content": "word " * 200, "truncated": False}
        for i in range(20)
    ]
    text, capped = _fit_trace_to_budget(_trace(big), 200)
    assert capped is True
    assert _count_tokens(text) <= 200


# --- judging ----------------------------------------------------------------


async def test_judge_parses_six_axes(monkeypatch):
    monkeypatch.setattr(traj, "get_llm_provider", lambda: _FakeProvider(_args()))
    out = await _judge_trajectory(_trace(), _llm(), max_input_tokens=10_000)
    assert out["status"] == "scored"
    assert len(out["axes"]) == len(AXES) == 6
    assert {a["key"] for a in out["axes"]} == {k for k, _, _ in AXES}
    assert out["overall_score"] == round((6 + 9 + 8 + 5 + 9 + 10) / 6, 2)
    assert out["loop_detected"] is False
    assert out["summary"].startswith("goal reached")
    assert out["judge_input_tokens"] == 120 and out["judge_output_tokens"] == 30
    assert out["judge_cost_usd"] > 0
    assert out["input_capped"] is False


async def test_judge_clamps_scores(monkeypatch):
    args = _args()
    args["efficiency"]["score"] = 99
    args["tool_selection"]["score"] = -5
    monkeypatch.setattr(traj, "get_llm_provider", lambda: _FakeProvider(args))
    out = await _judge_trajectory(_trace(), _llm(), max_input_tokens=10_000)
    by = {a["key"]: a["score"] for a in out["axes"]}
    assert by["efficiency"] == 10 and by["tool_selection"] == 0


async def test_judge_loop_detected_when_low(monkeypatch):
    monkeypatch.setattr(traj, "get_llm_provider", lambda: _FakeProvider(_args(loop=2)))
    out = await _judge_trajectory(_trace(), _llm(), max_input_tokens=10_000)
    assert out["loop_detected"] is True


async def test_judge_llm_failure_is_error_not_raise(monkeypatch):
    monkeypatch.setattr(traj, "get_llm_provider", lambda: _BoomProvider())
    out = await _judge_trajectory(_trace(), _llm(), max_input_tokens=10_000)
    assert out["status"] == "error" and "api down" in out["error"]


async def test_judge_bad_json_is_error(monkeypatch):
    monkeypatch.setattr(traj, "get_llm_provider", lambda: _BadJsonProvider())
    out = await _judge_trajectory(_trace(), _llm(), max_input_tokens=10_000)
    assert out["status"] == "error"


async def test_judge_missing_axis_scores_zero(monkeypatch):
    args = _args()
    del args["error_recovery"]
    monkeypatch.setattr(traj, "get_llm_provider", lambda: _FakeProvider(args))
    out = await _judge_trajectory(_trace(), _llm(), max_input_tokens=10_000)
    by = {a["key"]: a["score"] for a in out["axes"]}
    assert out["status"] == "scored" and by["error_recovery"] == 0


async def test_judge_bare_scalar_axis_is_tolerated(monkeypatch):
    # Some models emit a bare score per axis (``"efficiency": 8``) instead of the
    # ``{"score", "reason"}`` object; this must score, not crash into an error.
    args = {k: 8 for k, _, _ in AXES}
    args["summary"] = "flat axes"
    monkeypatch.setattr(traj, "get_llm_provider", lambda: _FakeProvider(args))
    out = await _judge_trajectory(_trace(), _llm(), max_input_tokens=10_000)
    by = {a["key"]: a["score"] for a in out["axes"]}
    assert out["status"] == "scored"
    assert all(v == 8 for v in by.values())
    assert out["overall_score"] == 8.0


async def test_judge_mixed_axis_shapes_are_tolerated(monkeypatch):
    # A response that mixes object axes with one bare-scalar axis must still parse.
    args = _args()
    args["efficiency"] = 10  # bare scalar alongside the object-shaped axes
    monkeypatch.setattr(traj, "get_llm_provider", lambda: _FakeProvider(args))
    out = await _judge_trajectory(_trace(), _llm(), max_input_tokens=10_000)
    by = {a["key"]: a["score"] for a in out["axes"]}
    assert out["status"] == "scored" and by["efficiency"] == 10
