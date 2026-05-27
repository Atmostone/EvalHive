"""Unit tests for the Failure Mode Classifier (E-14).

The LLM call is mocked (a fake provider returns a canned classify_failures tool
call), so these tests exercise the multi-label parsing/validation, the
input-budget cap and the error handling of `_classify_failures`, plus the pure
aggregation logic of `aggregate_failure_modes` (with a fake DB) — no network, no
real DB.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.quality import failure_modes as fm
from app.quality.failure_modes import (
    FAILURE_CLASS_KEYS,
    _classify_failures,
    _parse_failures_from_args,
    aggregate_failure_modes,
)


def _llm():
    return SimpleNamespace(
        model=SimpleNamespace(
            api_name="test-model", input_price_per_1m_usd=1, output_price_per_1m_usd=2
        ),
        provider=SimpleNamespace(api_key="k", endpoint="http://e/v1"),
    )


def _args(failures=None, summary="looped on search"):
    return {
        "failures": failures
        if failures is not None
        else [
            {"class": "loop", "confidence": 0.8, "reason": "repeated identical search 3×"},
            {"class": "ignored_error", "confidence": 0.6, "reason": "continued after 404"},
        ],
        "summary": summary,
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

    async def acompletion(self, **kw):
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
            {"seq": 0, "kind": "reasoning", "tool_name": None, "content": "plan", "truncated": False},
            {"seq": 1, "kind": "tool", "tool_name": "web_search", "content": "x", "truncated": False},
        ],
        "stats": {"original_tokens": 100, "cleaned_tokens": 50, "steps_total": 2},
    }


# --- parsing / validation ----------------------------------------------------


def test_parse_multi_label():
    out = _parse_failures_from_args(_args())
    assert [f["class"] for f in out] == ["loop", "ignored_error"]
    assert out[0]["confidence"] == 0.8


def test_parse_clamps_confidence():
    out = _parse_failures_from_args(
        _args(failures=[
            {"class": "loop", "confidence": 1.5, "reason": "a"},
            {"class": "ignored_error", "confidence": -0.2, "reason": "b"},
            {"class": "premature_stop", "confidence": "nan-ish", "reason": "c"},
        ])
    )
    by = {f["class"]: f["confidence"] for f in out}
    assert by["loop"] == 1.0 and by["ignored_error"] == 0.0 and by["premature_stop"] == 0.0


def test_parse_drops_unknown_class():
    out = _parse_failures_from_args(
        _args(failures=[
            {"class": "made_up", "confidence": 0.9, "reason": "x"},
            {"class": "loop", "confidence": 0.5, "reason": "y"},
        ])
    )
    assert [f["class"] for f in out] == ["loop"]


def test_parse_dedups_keeping_highest_confidence():
    out = _parse_failures_from_args(
        _args(failures=[
            {"class": "loop", "confidence": 0.4, "reason": "low"},
            {"class": "loop", "confidence": 0.9, "reason": "high"},
        ])
    )
    assert len(out) == 1 and out[0]["confidence"] == 0.9 and out[0]["reason"] == "high"


def test_parse_returns_taxonomy_order():
    # Provided out of order → output follows FAILURE_CLASS_KEYS order.
    out = _parse_failures_from_args(
        _args(failures=[
            {"class": "ignored_error", "confidence": 0.5, "reason": "a"},
            {"class": "tool_confusion", "confidence": 0.5, "reason": "b"},
        ])
    )
    assert [f["class"] for f in out] == ["tool_confusion", "ignored_error"]


# --- classifying -------------------------------------------------------------


async def test_classify_parses_failures(monkeypatch):
    monkeypatch.setattr(fm, "get_llm_provider", lambda: _FakeProvider(_args()))
    out = await _classify_failures(_trace(), None, None, _llm(), max_input_tokens=10_000)
    assert out["status"] == "scored"
    assert [f["class"] for f in out["failures"]] == ["loop", "ignored_error"]
    assert out["summary"].startswith("looped")
    assert out["judge_input_tokens"] == 120 and out["judge_output_tokens"] == 30
    assert out["judge_cost_usd"] > 0
    assert out["input_capped"] is False
    assert out["used_outcome_profile"] is False and out["used_trajectory_profile"] is False


async def test_classify_clean_run_empty(monkeypatch):
    monkeypatch.setattr(fm, "get_llm_provider", lambda: _FakeProvider(_args(failures=[], summary="clean")))
    out = await _classify_failures(_trace(), None, None, _llm(), max_input_tokens=10_000)
    assert out["status"] == "scored" and out["failures"] == []


async def test_classify_uses_context_profiles(monkeypatch):
    monkeypatch.setattr(fm, "get_llm_provider", lambda: _FakeProvider(_args()))
    outcome = {"weighted_score": 8.5}
    trajectory = {
        "status": "scored",
        "axes": [{"key": "loop_detection", "score": 2}],
        "loop_detected": True,
        "summary": "stuck",
    }
    out = await _classify_failures(_trace(), outcome, trajectory, _llm(), max_input_tokens=10_000)
    assert out["used_outcome_profile"] is True and out["used_trajectory_profile"] is True


async def test_classify_llm_failure_is_error(monkeypatch):
    monkeypatch.setattr(fm, "get_llm_provider", lambda: _BoomProvider())
    out = await _classify_failures(_trace(), None, None, _llm(), max_input_tokens=10_000)
    assert out["status"] == "error" and "api down" in out["error"]


async def test_classify_bad_json_is_error(monkeypatch):
    monkeypatch.setattr(fm, "get_llm_provider", lambda: _BadJsonProvider())
    out = await _classify_failures(_trace(), None, None, _llm(), max_input_tokens=10_000)
    assert out["status"] == "error"


async def test_classify_input_capped_on_big_trace(monkeypatch):
    monkeypatch.setattr(fm, "get_llm_provider", lambda: _FakeProvider(_args()))
    big = [
        {"seq": i, "kind": "tool", "tool_name": "t", "content": "word " * 200, "truncated": False}
        for i in range(20)
    ]
    out = await _classify_failures(_trace(big), None, None, _llm(), max_input_tokens=200)
    assert out["status"] == "scored" and out["input_capped"] is True


# --- aggregation -------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _q):
        return _FakeResult(self._rows)


def _rec(model, classes, status="scored"):
    return SimpleNamespace(
        model_used=model,
        template_name=None,
        template_id=None,
        failure_profile={
            "status": status,
            "failures": [{"class": c, "confidence": 0.7, "reason": ""} for c in classes],
        },
    )


async def test_aggregate_by_model_and_class():
    rows = [
        _rec("m1", ["loop", "ignored_error"]),
        _rec("m1", ["loop"]),
        _rec("m2", []),  # clean run
        _rec("m2", ["loop"], status="error"),  # skipped (not scored)
    ]
    out = await aggregate_failure_modes(_FakeDB(rows), workspace_id="ws")
    # 3 scored runs (the error one is skipped); 2 had failures.
    assert out["runs_total"] == 3 and out["failure_runs"] == 2
    assert out["by_class"]["loop"]["runs_total"] == 2
    assert out["by_model"]["m1"]["runs_total"] == 2
    assert out["by_model"]["m1"]["by_class"]["loop"] == 2
    assert out["by_model"]["m1"]["by_class"]["ignored_error"] == 1
    assert out["by_model"]["m2"]["failure_runs"] == 0
    assert out["rate"]["loop"] == round(2 / 3, 4)
    assert set(out["by_class"]["loop"]["by_class"].keys()) == set(FAILURE_CLASS_KEYS)


async def test_aggregate_failure_class_filter():
    rows = [
        _rec("m1", ["loop"]),
        _rec("m1", ["tool_confusion"]),
    ]
    out = await aggregate_failure_modes(_FakeDB(rows), workspace_id="ws", failure_class="loop")
    assert out["runs_total"] == 1 and out["by_class"]["loop"]["runs_total"] == 1
    assert "tool_confusion" not in out["by_class"]
