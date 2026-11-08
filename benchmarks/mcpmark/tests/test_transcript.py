"""Native message checks see the final answer, without reasoning or child output."""

import asyncio
import importlib.util
import json
import os
from types import SimpleNamespace

import pytest
from evalhive_benchmark import ProtocolError
from evalhive_mcpmark.transcript import Transcript, collect


def event(content, *, message="answer", **changes):
    return {
        "event": "message.end",
        "source": "fixture",
        "session_id": "session",
        "data": {"role": "assistant", "message_id": message, "content": content},
        **changes,
    }


def test_snapshots_reasoning_and_child_sessions_do_not_replace_the_final_main_answer():
    transcript = Transcript()
    transcript.add(event("Partial"))
    transcript.add(event("Final answer"))
    reasoning = event("Private reasoning", message="answer")
    reasoning["data"]["channel"] = "reasoning"
    transcript.add(reasoning)
    transcript.add(event("Child answer", parent_session_id="session", session_id="child"))
    (message,) = transcript.messages()
    assert message["status"] == "completed" and message["type"] == "message"
    assert message["content"] == [{"type": "output_text", "text": "Final answer"}]


def test_original_browser_checker_reads_the_same_confirmed_final_answer(tmp_path, monkeypatch):
    from pathlib import Path

    root = os.getenv("EVALHIVE_MCPMARK_NATIVE_ROOT")
    if not root:
        pytest.skip("Requires the pinned original MCPMark checker")
    path = (
        Path(root) / "tasks/playwright/standard/eval_web/cloudflare_turnstile_challenge/verify.py"
    )
    spec = importlib.util.spec_from_file_location("native_browser_message_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    transcript = Transcript()
    transcript.add(event("The original checker can read this answer"))
    messages = tmp_path / "messages.json"
    messages.write_text(json.dumps(transcript.messages()))
    monkeypatch.setenv("MCP_MESSAGES", str(messages))
    assert module.get_model_response() == "The original checker can read this answer"


def test_terminal_flag_does_not_skip_remaining_event_pages():
    calls = []
    rows = [event("First", message="first"), event("Final", message="second")]

    async def agent(scope, operation, payload, **options):
        calls.append(payload["cursor"])
        cursor = payload["cursor"]
        return {
            "events": rows[cursor : cursor + 1],
            "cursor": min(cursor + 1, len(rows)),
            "closed": True,
        }

    result = asyncio.run(
        collect(SimpleNamespace(agent=agent), {}, {"outcome": {"attempt_id": "session"}}, timeout=5)
    )
    assert calls == [0, 1, 2] and result["through_seq"] == 1
    assert len(result["messages"]) == 2


def test_missing_event_cannot_be_hidden_by_a_terminal_cursor():
    async def agent(*args, **options):
        return {"events": [], "cursor": 1, "closed": True}

    with pytest.raises(ProtocolError) as error:
        asyncio.run(
            collect(
                SimpleNamespace(agent=agent), {}, {"outcome": {"attempt_id": "session"}}, timeout=5
            )
        )
    assert error.value.code == "NATIVE_TRANSCRIPT_GAP"


def tool(kind, identity, *, status="completed", session="session"):
    return {
        "event": "tool." + kind,
        "source": "fixture",
        "session_id": session,
        "data": {"call_id": identity, "status": status},
    }


def test_interleaved_nested_tools_use_call_and_session_identity():
    transcript = Transcript()
    for event in [
        tool("start", "a"),
        tool("start", "a", session="child"),
        tool("end", "a", session="child"),
        tool("end", "a"),
    ]:
        transcript.add(event)
    assert not transcript.uncertain_tools and not transcript.pending_tools


@pytest.mark.parametrize(
    "events",
    [
        [tool("start", "missing-end")],
        [tool("end", "missing-start")],
        [tool("start", "failed"), tool("end", "failed", status="failed")],
        [tool("start", None), tool("end", None)],
        [
            tool("start", "reuse"),
            tool("end", "reuse"),
            tool("start", "reuse"),
            tool("end", "reuse"),
        ],
    ],
)
def test_unknown_or_failed_tool_effects_prevent_cleanup_confidence(events):
    transcript = Transcript()
    for event in events:
        transcript.add(event)
    assert transcript.uncertain_tools or transcript.pending_tools
