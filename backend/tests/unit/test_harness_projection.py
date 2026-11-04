"""Normalized evidence must not multiply tokens or invent missing measurements."""

import pytest
from app.harnesses.projection import project_log, summarize_usage


def event(seq, kind, data, session=None):
    return {"attempt_id": "a", "session_id": session, "seq": seq, "event": kind, "data": data}


def usage(seq, identity="call", **fields):
    return event(
        seq,
        "usage",
        {
            "usage_id": identity,
            "mode": "cumulative",
            "model": "model",
            "purpose": "agent",
            **fields,
        },
    )


def test_cumulative_usage_replaces_progress_and_native_totals_are_not_added():
    report = summarize_usage(
        [
            usage(0, input_tokens=10, output_tokens=1),
            usage(1, input_tokens=10, output_tokens=5),
            event(2, "native", {"payload": {"usage": {"input_tokens": 10, "output_tokens": 5}}}),
        ]
    )
    assert report["input_tokens"] == 10 and report["output_tokens"] == 5
    assert report["calls_reported"] == 1
    assert report["reasoning_tokens"] is None and report["cache_read_tokens"] is None
    assert report["cost_usd"] is None


def test_delta_usage_sums_exact_cost_and_preserves_partial_unknowns():
    events = [
        usage(
            0,
            mode="delta",
            input_tokens=2,
            output_tokens=1,
            cost={"amount": "0.0001", "currency": "USD", "source": "provider"},
        ),
        usage(
            1,
            mode="delta",
            input_tokens=3,
            output_tokens=2,
            cost={"amount": "0.0002", "currency": "USD", "source": "provider"},
        ),
    ]
    report = summarize_usage(events)
    assert report["input_tokens"] == 5 and report["output_tokens"] == 3
    assert report["cost_usd"] == "0.0003" and report["cost_sources"] == ["provider"]
    assert report["cache_write_tokens"] is None


def test_auxiliary_model_unknown_usage_stays_unknown_in_total():
    report = summarize_usage(
        [
            usage(0, input_tokens=10, output_tokens=2),
            usage(1, "compaction", model="small", purpose="compaction", output_tokens=3),
        ]
    )
    assert report["input_tokens"] is None and report["output_tokens"] == 5
    assert len(report["by_model_and_purpose"]) == 2
    assert report["calls_reported"] == 2


def test_missing_model_in_progress_does_not_create_another_usage_identity():
    report = summarize_usage(
        [
            usage(0, model=None, input_tokens=1),
            usage(1, input_tokens=5, output_tokens=1),
        ]
    )
    assert report["input_tokens"] == 5 and report["calls_reported"] == 1
    assert report["by_model_and_purpose"][0]["model"] == "model"


@pytest.mark.parametrize(
    "fields", [{"mode": "delta"}, {"model": "another"}, {"purpose": "compaction"}]
)
def test_conflicting_usage_identity_is_explicit(fields):
    with pytest.raises(ValueError, match="Usage"):
        summarize_usage([usage(0), usage(1, **fields)])


def test_interleaved_calls_and_nested_sessions_keep_their_own_results():
    events = [
        event(
            0,
            "tool.start",
            {"call_id": "same", "name": "write", "arguments": {"path": "a"}},
            "root",
        ),
        event(
            1,
            "tool.start",
            {
                "call_id": "same",
                "name": "read",
                "arguments": {"path": "b"},
                "parent_call_id": "same",
            },
            "child",
        ),
        event(2, "tool.update", {"call_id": "same", "content": "partial", "mode": "delta"}, "root"),
        event(3, "tool.end", {"call_id": "same", "result": "B", "status": "completed"}, "child"),
        event(4, "tool.end", {"call_id": "same", "result": "A", "status": "completed"}, "root"),
    ]
    rows = project_log(events, tool_aliases={"write": "file_write"})
    assert len(rows) == 2 and [row["content"] for row in rows] == ["A", "B"]
    assert rows[0]["tool_name"] == "file_write" and rows[0]["native_tool_name"] == "write"
    assert rows[1]["parent_call_id"] == "same" and rows[1]["session_id"] == "child"
    assert not any(row["result_missing"] or row["start_missing"] for row in rows)


def test_incomplete_tool_output_does_not_become_a_completed_result():
    rows = project_log(
        [
            event(0, "tool.start", {"call_id": "c", "name": "bash", "arguments": {}}),
            event(
                1, "tool.update", {"call_id": "c", "content": "Still running", "mode": "snapshot"}
            ),
        ]
    )
    assert rows[0]["result_missing"] and rows[0]["content"] == "Still running"


def test_message_end_is_authoritative_and_reasoning_is_separate():
    rows = project_log(
        [
            event(0, "message.delta", {"message_id": "m", "role": "assistant", "delta": "Par"}),
            event(1, "message.end", {"message_id": "m", "role": "assistant", "content": "Final"}),
            event(
                2,
                "message.end",
                {
                    "message_id": "m",
                    "role": "assistant",
                    "channel": "reasoning",
                    "content": "Thought",
                },
            ),
        ]
    )
    assert len(rows) == 2 and rows[0]["content"] == "Final"
    assert rows[1]["reasoning"] == "Thought" and rows[1]["content"] == ""


def test_absent_usage_is_unknown():
    report = summarize_usage([])
    assert not report["reported"] and report["input_tokens"] is None and report["cost_usd"] is None
