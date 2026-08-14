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


def _chunk(
    content,
    tool_name="tool",
    seq=0,
    secs=None,
    arguments=None,
    arguments_truncated=False,
    tool_call_id=None,
    part_index=0,
    part_total=1,
):
    created = _BASE + timedelta(seconds=secs) if secs is not None else None
    return SimpleNamespace(
        content=content,
        tool_name=tool_name,
        chunk_seq=seq,
        created_at=created,
        arguments=arguments,
        arguments_truncated=arguments_truncated,
        tool_call_id=tool_call_id,
        part_index=part_index,
        part_total=part_total,
    )


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


# --- tool-call arguments (SPA-86) ------------------------------------------


def test_arguments_reach_the_cleaned_step():
    trace = clean_trajectory(
        _task(), [], [_chunk("ok", tool_name="write_file", arguments={"path": "out/a.md"})]
    )
    step = trace["steps"][0]
    assert step["arguments"] == {"path": "out/a.md"}
    assert step["arguments_truncated"] is False


def test_long_argument_value_is_truncated_but_no_key_is_dropped():
    """A file body can arrive as a parameter. Which parameters were passed is the
    signal, so values shrink and keys stay."""
    args = {"path": "out/a.md", "content": "word " * 5000, "mode": "overwrite"}
    trace = clean_trajectory(
        _task(),
        [],
        [_chunk("ok", tool_name="write_file", arguments=args)],
        config=TraceCleanerConfig(tool_args_token_cap=100),
    )
    step = trace["steps"][0]
    assert set(step["arguments"]) == {"path", "content", "mode"}
    assert step["arguments"]["path"] == "out/a.md"
    assert "truncated" in step["arguments"]["content"]
    assert step["arguments_truncated"] is True
    assert trace["stats"]["steps_args_truncated"] == 1


def test_agent_side_truncation_flag_survives_even_when_the_cap_does_not_fire():
    trace = clean_trajectory(
        _task(),
        [],
        [_chunk("ok", arguments={"p": "short"}, arguments_truncated=True)],
    )
    assert trace["steps"][0]["arguments_truncated"] is True


def test_args_cap_zero_disables_argument_truncation():
    args = {"content": "word " * 5000}
    trace = clean_trajectory(
        _task(),
        [],
        [_chunk("ok", arguments=args)],
        config=TraceCleanerConfig(tool_args_token_cap=0),
    )
    step = trace["steps"][0]
    assert step["arguments"] == args
    assert step["arguments_truncated"] is False


def test_output_cap_zero_disables_output_truncation():
    """With a 1M-context judge, «no truncation» has to be reachable (SPA-86)."""
    content = "word " * 5000
    trace = clean_trajectory(
        _task(), [], [_chunk(content)], config=TraceCleanerConfig(tool_output_token_cap=0)
    )
    step = trace["steps"][0]
    assert step["truncated"] is False
    assert step["content"] == content
    assert trace["stats"]["steps_truncated"] == 0
    assert trace["config"]["tool_output_token_cap"] == 0


def test_garbage_cap_falls_back_to_the_default_rather_than_disabling_it():
    """Off has to be asked for explicitly — a malformed setting must not silently
    remove the cost cap from every run."""
    trace = clean_trajectory(
        _task(), [], [_chunk("word " * 5000)], config=TraceCleanerConfig(tool_output_token_cap="oops")
    )
    assert trace["config"]["tool_output_token_cap"] == 600
    assert trace["steps"][0]["truncated"] is True


# --- one call, one step ------------------------------------------------------


def test_split_output_parts_join_into_one_step():
    """An output over the agent's transport cap arrives as several rows. Left
    alone they read as N separate calls — each capped and counted separately."""
    chunks = [
        _chunk("part-A ", seq=0, tool_call_id="call_1", part_index=0, part_total=3,
               arguments={"path": "big.csv"}),
        _chunk("part-B ", seq=1, tool_call_id="call_1", part_index=1, part_total=3,
               arguments={"path": "big.csv"}),
        _chunk("part-C", seq=2, tool_call_id="call_1", part_index=2, part_total=3,
               arguments={"path": "big.csv"}),
    ]
    trace = clean_trajectory(_task(), [], chunks)
    assert trace["stats"]["steps_total"] == 1
    assert trace["steps"][0]["content"] == "part-A part-B part-C"
    assert trace["steps"][0]["arguments"] == {"path": "big.csv"}


def test_parts_are_joined_in_part_order_not_arrival_order():
    chunks = [
        _chunk("B", seq=0, tool_call_id="c", part_index=1, part_total=2),
        _chunk("A", seq=1, tool_call_id="c", part_index=0, part_total=2),
    ]
    trace = clean_trajectory(_task(), [], chunks)
    assert trace["steps"][0]["content"] == "AB"


def test_distinct_calls_are_never_merged():
    chunks = [
        _chunk("one", tool_call_id="call_1"),
        _chunk("two", tool_call_id="call_2"),
    ]
    trace = clean_trajectory(_task(), [], chunks)
    assert trace["stats"]["steps_total"] == 2


def test_non_consecutive_reuse_of_a_call_id_is_not_merged():
    """A provider that recycles ids across turns must not have two genuinely
    different calls fused into one step."""
    chunks = [
        _chunk("one", tool_call_id="c"),
        _chunk("other", tool_call_id="d"),
        _chunk("three", tool_call_id="c"),
    ]
    trace = clean_trajectory(_task(), [], chunks)
    assert trace["stats"]["steps_total"] == 3


def test_chunks_without_a_call_id_keep_one_step_each():
    trace = clean_trajectory(_task(), [], [_chunk("a"), _chunk("b")])
    assert trace["stats"]["steps_total"] == 2


# --- gaps are marked, never spliced over (SPA-86 review) ---------------------


def test_missing_output_part_is_marked_not_spliced():
    """Part 1 never reached the backend (the agent suppresses failed POSTs).
    Joining 0 to 2 would fabricate a contiguous output that never existed."""
    chunks = [
        _chunk("AAA", seq=0, tool_call_id="c", part_index=0, part_total=3),
        _chunk("CCC", seq=1, tool_call_id="c", part_index=2, part_total=3),
    ]
    trace = clean_trajectory(_task(), [], chunks, config=TraceCleanerConfig(tool_output_token_cap=0))
    step = trace["steps"][0]
    assert step["parts_missing"] == 1
    assert "part 1" in step["content"] and "not recorded" in step["content"]
    assert step["content"].startswith("AAA")
    assert step["content"].endswith("CCC")
    assert trace["stats"]["steps_parts_missing"] == 1


def test_call_with_no_result_is_reported_as_such():
    """The agent records the call BEFORE running the tool, so a hang or a raising
    builtin leaves part 0 alone. The call must survive and say it has no result."""
    chunks = [_chunk("", seq=0, tool_call_id="c", part_index=0, part_total=1,
                     arguments={"path": "a.txt"})]
    trace = clean_trajectory(_task(), [], chunks)
    step = trace["steps"][0]
    assert step["result_missing"] is True
    assert step["arguments"] == {"path": "a.txt"}
    assert "no result recorded" in step["content"]
    assert trace["stats"]["steps_result_missing"] == 1


def test_call_plus_result_is_one_complete_step():
    chunks = [
        _chunk("", seq=0, tool_call_id="c", part_index=0, part_total=1, arguments={"p": 1}),
        _chunk("done", seq=1, tool_call_id="c", part_index=1, part_total=2, arguments={"p": 1}),
    ]
    trace = clean_trajectory(_task(), [], chunks)
    step = trace["steps"][0]
    assert trace["stats"]["steps_total"] == 1
    assert step["result_missing"] is False
    assert step["parts_missing"] == 0
    assert step["content"] == "done"


def test_a_tool_returning_empty_output_is_not_a_missing_result():
    """An empty result is a result: the row that carries it announces part_total 2,
    which is what distinguishes it from a call that never returned."""
    chunks = [
        _chunk("", seq=0, tool_call_id="c", part_index=0, part_total=1, arguments={"p": 1}),
        _chunk("", seq=1, tool_call_id="c", part_index=1, part_total=2, arguments={"p": 1}),
    ]
    trace = clean_trajectory(_task(), [], chunks)
    assert trace["steps"][0]["result_missing"] is False


def test_legacy_single_chunk_with_a_call_id_is_untouched():
    """Pre-review rows put the result itself in part 0; they must not be read as
    results that went missing."""
    chunks = [_chunk("output", seq=0, tool_call_id="c", part_index=0, part_total=1)]
    trace = clean_trajectory(_task(), [], chunks)
    assert trace["steps"][0]["result_missing"] is False
    assert trace["steps"][0]["content"] == "output"
