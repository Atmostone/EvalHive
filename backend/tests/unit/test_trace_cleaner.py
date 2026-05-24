"""Unit tests for the Trace Cleaner (E-06).

The cleaner is a pure function over trajectory inputs (it reads attributes via
getattr), so these tests use lightweight SimpleNamespace stand-ins for the
task / events / log chunks rather than DB rows.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.quality.trace_cleaner import (
    TRACE_SCHEMA_VERSION,
    TraceCleanerConfig,
    _count_tokens,
    _truncate_to_tokens,
    clean_trajectory,
)

_BASE = datetime(2026, 1, 1, 12, 0, 0)


def _ev(event_type, data, secs=0):
    return SimpleNamespace(event_type=event_type, data=data, created_at=_BASE + timedelta(seconds=secs))


def _chunk(content, tool_name="tool", seq=0, secs=None):
    created = _BASE + timedelta(seconds=secs) if secs is not None else None
    return SimpleNamespace(content=content, tool_name=tool_name, chunk_seq=seq, created_at=created)


def _task(**kw):
    kw.setdefault("id", "task-1")
    kw.setdefault("title", "Title")
    kw.setdefault("description", "Desc")
    kw.setdefault("status", "done")
    return SimpleNamespace(**kw)


# --- token helpers --------------------------------------------------------


def test_count_tokens():
    assert _count_tokens("") == 0
    assert _count_tokens("hello world") > 0


def test_truncate_under_cap_is_noop():
    head, dropped = _truncate_to_tokens("a few words", 1000)
    assert head == "a few words" and dropped == 0


def test_truncate_over_cap():
    text = "word " * 300
    head, dropped = _truncate_to_tokens(text, 20)
    assert dropped > 0
    assert _count_tokens(head) <= 20


# --- filtering ------------------------------------------------------------


def test_drops_system_snapshot_and_noise():
    events = [
        _ev("agent_spawned", {"soul_md": "x" * 5000, "tools": ["a", "b"]}, secs=0),
        _ev("agent_health", {"status": "ok"}, secs=1),
        _ev("task_status_changed", {"new_status": "done"}, secs=2),
        _ev("orchestrator_reasoning", {"decision": "select", "reasoning": "best fit"}, secs=3),
    ]
    trace = clean_trajectory(_task(), events, [])
    kinds = [s["kind"] for s in trace["steps"]]
    assert kinds == ["reasoning"]  # only the reasoning event survives
    assert trace["stats"]["events_dropped"] == 3
    assert "best fit" in trace["steps"][0]["content"]


def test_chronological_merge_of_events_and_chunks():
    events = [
        _ev("orchestrator_reasoning", {"reasoning": "think"}, secs=1),
        _ev("agent_progress", {"message": "working"}, secs=3),
    ]
    chunks = [_chunk("tool output", tool_name="web_search", seq=0, secs=2)]
    trace = clean_trajectory(_task(), events, chunks)
    assert [s["kind"] for s in trace["steps"]] == ["reasoning", "tool", "agent"]
    assert [s["seq"] for s in trace["steps"]] == [0, 1, 2]
    assert trace["steps"][1]["tool_name"] == "web_search"


# --- truncation -----------------------------------------------------------


def test_tool_output_truncated_at_cap():
    chunks = [_chunk("word " * 300, tool_name="web", seq=0, secs=1)]
    trace = clean_trajectory(_task(), [], chunks, config=TraceCleanerConfig(tool_output_token_cap=50))
    step = trace["steps"][0]
    assert step["truncated"] is True
    assert step["kept_tokens"] == 50
    assert "[truncated" in step["content"]
    assert trace["stats"]["steps_truncated"] == 1


def test_reasoning_not_truncated():
    # reasoning steps are kept in full even past the cap (the judge needs the "why")
    events = [_ev("orchestrator_reasoning", {"reasoning": "word " * 300}, secs=1)]
    trace = clean_trajectory(_task(), events, [], config=TraceCleanerConfig(tool_output_token_cap=20))
    assert trace["steps"][0]["truncated"] is False


def test_keep_tail_on_error_preserves_full_step():
    content = "Traceback (most recent call last): " + "x " * 300
    chunks = [_chunk(content, tool_name="run", seq=0, secs=1)]
    cfg_off = TraceCleanerConfig(tool_output_token_cap=20, keep_tail_on_error=False)
    cfg_on = TraceCleanerConfig(tool_output_token_cap=20, keep_tail_on_error=True)

    assert clean_trajectory(_task(), [], chunks, config=cfg_off)["steps"][0]["truncated"] is True
    # error step kept whole when the option is on
    kept = clean_trajectory(_task(), [], chunks, config=cfg_on)["steps"][0]
    assert kept["truncated"] is False and "[truncated" not in kept["content"]


# --- stats & robustness ---------------------------------------------------


def test_savings_positive_on_noisy_trace():
    events = [_ev("agent_spawned", {"soul_md": "noise " * 1000}, secs=0)]
    chunks = [_chunk("real " * 200, tool_name="t", seq=0, secs=1)]
    trace = clean_trajectory(_task(), events, chunks, config=TraceCleanerConfig(tool_output_token_cap=30))
    assert trace["stats"]["savings_tokens"] > 0
    assert trace["stats"]["savings_pct"] > 0


def test_empty_inputs_produce_empty_trace():
    trace = clean_trajectory(_task(), [], [])
    assert trace["steps"] == []
    assert trace["stats"]["steps_total"] == 0
    assert trace["schema_version"] == TRACE_SCHEMA_VERSION


def test_malformed_event_data_does_not_raise():
    events = [
        _ev("orchestrator_reasoning", "not a dict", secs=1),
        _ev("agent_progress", None, secs=2),
        _ev("orchestrator_decision", 12345, secs=3),
    ]
    chunks = [_chunk(None, tool_name=None, seq=0, secs=4)]
    trace = clean_trajectory(_task(), events, chunks)
    assert "error" not in trace
    assert trace["stats"]["steps_total"] == 4


def test_cap_clamped_to_bounds():
    chunks = [_chunk("word " * 100, tool_name="t", seq=0, secs=1)]
    # absurdly low cap is clamped up to TOKEN_CAP_MIN (50), not 1
    trace = clean_trajectory(_task(), [], chunks, config=TraceCleanerConfig(tool_output_token_cap=1))
    assert trace["config"]["tool_output_token_cap"] == 50
