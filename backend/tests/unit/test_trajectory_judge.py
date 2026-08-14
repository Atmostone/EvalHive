"""Unit tests for the 6-axis Trajectory Judge (E-07).

The LLM call is mocked (a fake provider returns a canned score_trajectory tool
call), so these tests exercise prompt serialization, the input-budget cap, and
the parsing/clamping/error handling of `_judge_trajectory` without a network or
a DB.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.quality import trajectory as traj
from app.quality.trace_cleaner import _count_tokens
from app.quality.trajectory import (
    AXES,
    _extract_tool_args,
    _judge_trajectory,
    _loads_lenient,
    _serialize_trace,
    fit_trace_to_budget,
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


def test_serialize_renders_tool_call_arguments():
    """The call, not just its name and result — `parameter_quality` cannot be
    scored on evidence that contains no parameters (SPA-86)."""
    steps = [
        {
            "seq": 0,
            "kind": "tool",
            "tool_name": "write_file",
            "arguments": {"path": "out/report.md", "content": "hello"},
            "content": "ok",
            "truncated": False,
        }
    ]
    text = _serialize_trace(_trace(steps))
    assert "out/report.md" in text and "args=" in text


def test_serialize_flags_truncated_arguments():
    steps = [
        {
            "seq": 0,
            "kind": "tool",
            "tool_name": "write_file",
            "arguments": {"path": "a", "content": "x…[truncated 900 chars]…"},
            "arguments_truncated": True,
            "content": "ok",
            "truncated": False,
        }
    ]
    assert "[args truncated]" in _serialize_trace(_trace(steps))


def test_fit_within_budget_not_capped():
    text, trim = fit_trace_to_budget(_trace(), 10_000)
    assert trim["capped"] is False and trim["mode"] == "budget" and "Build X" in text


def test_fit_with_no_budget_trims_nothing():
    """`max_input_tokens = 0` is the off sentinel: a 1M-context judge can be asked
    to read the whole trajectory, and the profile has to say that it did."""
    big = [
        {"seq": i, "kind": "tool", "tool_name": "t", "content": "word " * 500, "truncated": False}
        for i in range(20)
    ]
    text, trim = fit_trace_to_budget(_trace(big), 0)
    assert trim["mode"] == "none" and trim["capped"] is False
    assert trim["steps_omitted"] == 0 and trim["hard_cut_tokens"] == 0
    assert _count_tokens(text) > 5_000  # nothing was removed


def test_fit_spends_tool_outputs_before_dropping_steps():
    """Order of sacrifice: an output is cheaper than a whole step. A budget that
    can be met by shrinking outputs must not evict steps to get there."""
    big = [
        {"seq": i, "kind": "tool", "tool_name": f"t{i}", "content": "word " * 400, "truncated": False}
        for i in range(6)
    ]
    text, trim = fit_trace_to_budget(_trace(big), 900)
    assert trim["capped"] is True
    assert trim["outputs_shrunk"] > 0
    assert trim["steps_omitted"] == 0
    # Every call survives, which is the point — the calls are the evidence.
    for i in range(6):
        assert f"t{i}" in text


def test_fit_shrinks_reasoning_only_after_outputs():
    steps = [
        {"seq": 0, "kind": "reasoning", "tool_name": None, "content": "think " * 400, "truncated": False},
        {"seq": 1, "kind": "tool", "tool_name": "t", "content": "word " * 400, "truncated": False},
    ]
    _text, trim = fit_trace_to_budget(_trace(steps), 300)
    assert trim["outputs_shrunk"] > 0
    assert trim["reasoning_shrunk"] > 0
    assert trim["output_cap_applied"] is not None


def test_fit_over_budget_drops_middle_steps_with_signatures():
    big = [
        {"seq": i, "kind": "tool", "tool_name": "t", "content": "word " * 200, "truncated": False}
        for i in range(20)
    ]
    text, trim = fit_trace_to_budget(_trace(big), 200)
    assert trim["capped"] is True
    assert _count_tokens(text) <= 200
    assert trim["steps_omitted"] > 0
    # The judge is told WHAT went, not merely how much (canonical plan E4).
    assert "t×" in trim["omitted_signatures"]


def test_hard_cut_is_marked_not_silent():
    """The last resort used to cut the text and only set a flag the judge never
    sees. Now the text says so where the judge reads it."""
    steps = [
        {"seq": 0, "kind": "tool", "tool_name": "t", "content": "word " * 5000, "truncated": False},
        {"seq": 1, "kind": "tool", "tool_name": "t", "content": "word " * 5000, "truncated": False},
    ]
    text, trim = fit_trace_to_budget(_trace(steps), 120)
    assert trim["hard_cut_tokens"] > 0
    assert "hard-cut" in text


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


async def test_judge_not_applicable_axis_excluded_and_renormalizes(monkeypatch):
    # An axis the judge marks applicable=false is excluded from the aggregate:
    # the overall divides by the SCORED-axis count, not the fixed len(AXES), so
    # the N/A axis does not drag the mean toward 0.
    args = _args()  # eff 6, tool_sel 9, param 8, err 5, goal 9, loop 10 → sum 47
    args["error_recovery"] = {"applicable": False, "reason": "no tool errors occurred"}
    monkeypatch.setattr(traj, "get_llm_provider", lambda: _FakeProvider(args))
    out = await _judge_trajectory(_trace(), _llm(), max_input_tokens=10_000)
    assert out["status"] == "scored"
    by = {a["key"]: a for a in out["axes"]}
    # excluded axis is preserved in the list, marked N/A, with score None
    assert by["error_recovery"]["status"] == "not_applicable"
    assert by["error_recovery"]["score"] is None
    # overall = (6+9+8+9+10) / 5 scored axes (NOT /6)
    assert out["overall_score"] == round((6 + 9 + 8 + 9 + 10) / 5, 2)


async def test_judge_na_loop_axis_does_not_flip_badge(monkeypatch):
    # loop_detection marked N/A (no real activity) must NOT flip the loop badge,
    # even though its (absent) score is below the threshold.
    args = _args()
    args["loop_detection"] = {"applicable": False, "reason": "crashed at step 1"}
    monkeypatch.setattr(traj, "get_llm_provider", lambda: _FakeProvider(args))
    out = await _judge_trajectory(_trace(), _llm(), max_input_tokens=10_000)
    assert out["loop_detected"] is False
    by = {a["key"]: a for a in out["axes"]}
    assert by["loop_detection"]["status"] == "not_applicable"


async def test_judge_all_axes_na_yields_none_overall(monkeypatch):
    # If every axis is N/A (nothing to judge), overall is None, not 0.
    args = {k: {"applicable": False, "reason": "n/a"} for k, _, _ in AXES}
    args["summary"] = "no activity"
    monkeypatch.setattr(traj, "get_llm_provider", lambda: _FakeProvider(args))
    out = await _judge_trajectory(_trace(), _llm(), max_input_tokens=10_000)
    assert out["status"] == "scored"
    assert out["overall_score"] is None
    assert all(a["score"] is None for a in out["axes"])


# --- lenient JSON parsing + retry (E-07 robustness, ~27% of evals were erroring) ---


def test_loads_lenient_plain_and_fenced():
    obj = {"efficiency": {"score": 7}, "summary": "ok"}
    assert _loads_lenient(json.dumps(obj)) == obj
    assert _loads_lenient("```json\n" + json.dumps(obj) + "\n```") == obj
    assert _loads_lenient("```\n" + json.dumps(obj) + "\n```") == obj


def test_loads_lenient_strips_prose_and_trailing_junk():
    obj = {"a": 1, "b": "x"}
    assert _loads_lenient("Here is the score: " + json.dumps(obj)) == obj
    assert _loads_lenient(json.dumps(obj) + "\n\nThat is my assessment.") == obj


def test_loads_lenient_closes_cleanly_truncated_object():
    # response cut off after a complete value — the missing braces are added back
    out = _loads_lenient('{"efficiency": {"score": 6}, "summary": "done"')
    assert out["efficiency"]["score"] == 6 and out["summary"] == "done"


def test_loads_lenient_raises_on_unrecoverable():
    for bad in ("", "   ", "no json object here"):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            _loads_lenient(bad)


def test_extract_tool_args_falls_back_to_content():
    # provider ignored the forced tool call → JSON arrived in message.content
    obj = {"efficiency": {"score": 8}}
    choice = SimpleNamespace(tool_calls=None, content=json.dumps(obj))
    assert _extract_tool_args(choice) == obj


class _FlakyProvider:
    """Malformed tool-call JSON on the first call, valid on the retry."""

    def __init__(self, good_args):
        self.calls = 0
        self._good = good_args

    async def acompletion(self, **kw):
        self.calls += 1
        if self.calls == 1:  # unrecoverable: no JSON object at all → forces a retry
            return MagicMock(
                choices=[
                    MagicMock(
                        message=SimpleNamespace(
                            tool_calls=None, content="garbled — no json object here"
                        )
                    )
                ],
                usage={},
            )
        return _resp(self._good)


async def test_judge_retries_and_recovers_from_transient_bad_json(monkeypatch):
    prov = _FlakyProvider(_args())
    monkeypatch.setattr(traj, "get_llm_provider", lambda: prov)
    out = await _judge_trajectory(_trace(), _llm(), max_input_tokens=10_000)
    assert out["status"] == "scored"
    assert prov.calls == 2  # first attempt failed to parse, retry succeeded


# --- the trim must own the cleaner's losses too (SPA-86 review) --------------


def _capped_trace():
    """A trace the CLEANER already truncated, small enough to fit any budget."""
    return {
        "task": {"id": "t", "title": "T", "description": "D"},
        "steps": [
            {"seq": 0, "kind": "tool", "tool_name": "bash", "content": "short",
             "truncated": True, "original_tokens": 3000, "kept_tokens": 600},
        ],
        "stats": {"steps_truncated": 1, "steps_args_truncated": 1},
    }


def test_trim_reports_what_the_cleaner_removed_before_the_budget():
    """A run whose outputs were capped at 600 tokens fits the budget easily — and
    used to report «nothing removed», hiding thousands of lost tokens and making
    two runs with different evidence look identically untouched."""
    _text, trim = fit_trace_to_budget(_capped_trace(), 12_000)
    assert trim["capped"] is False           # the budget itself removed nothing
    assert trim["anything_removed"] is True  # …but something was removed
    assert trim["pre_trim_outputs_truncated"] == 1
    assert trim["pre_trim_args_truncated"] == 1
    assert trim["pre_trim_dropped_tokens"] == 2400


def test_untouched_trace_reports_nothing_removed():
    _text, trim = fit_trace_to_budget(_trace(), 12_000)
    assert trim["anything_removed"] is False
    assert trim["pre_trim_dropped_tokens"] == 0


def test_cleaner_losses_are_reported_even_with_no_budget():
    _text, trim = fit_trace_to_budget(_capped_trace(), 0)
    assert trim["mode"] == "none"
    assert trim["anything_removed"] is True
    assert trim["pre_trim_dropped_tokens"] == 2400


def test_serializer_flags_a_call_with_no_result():
    steps = [{"seq": 0, "kind": "tool", "tool_name": "bash", "arguments": {"c": "x"},
              "result_missing": True, "content": "…[no result recorded…]…", "truncated": False}]
    assert "[no result recorded]" in _serialize_trace(_trace(steps))


def test_serializer_flags_missing_output_parts():
    steps = [{"seq": 0, "kind": "tool", "tool_name": "bash", "arguments": {"c": "x"},
              "parts_missing": 2, "content": "a…b", "truncated": False}]
    assert "[2 output part(s) missing]" in _serialize_trace(_trace(steps))
