import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest
from evalhive_harness.client import ConnectorClient
from evalhive_harness.connector import Connector, serve
from evalhive_harness.protocol import (
    ProtocolError,
    command,
    decode_line,
    encode_line,
    validate,
    validate_run,
)

ROOT = Path(__file__).parents[2]
FIXTURE = json.loads((ROOT / "fixtures/conversation.json").read_text())


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_shared_conversation_shapes(case):
    if case["error"]:
        with pytest.raises(ProtocolError) as caught:
            validate(case["kind"], case["value"])
        assert caught.value.code == case["error"]
    else:
        assert validate(case["kind"], case["value"]) == case["value"]


def test_interaction_versions_and_capabilities_are_preflight_errors():
    spec = copy.deepcopy(FIXTURE["spec"])
    validate_run(FIXTURE["manifest"], spec)
    spec["interaction"]["version"] = "2.0"
    with pytest.raises(ProtocolError) as caught:
        validate_run(FIXTURE["manifest"], spec)
    assert caught.value.code == "UNSUPPORTED_INTERACTION_VERSION"
    spec["interaction"]["version"] = "1.0"
    manifest = copy.deepcopy(FIXTURE["manifest"])
    del manifest["capabilities"]["external_tool_execution"]
    with pytest.raises(ProtocolError) as caught:
        validate_run(manifest, spec)
    assert caught.value.code == "UNSUPPORTED_CAPABILITY"


@pytest.mark.parametrize("cancelled", [False, True])
def test_connector_cannot_report_success_before_session_close(cancelled):
    async def scenario():
        class Premature(Connector):
            manifest = FIXTURE["manifest"]

            async def run(self, context):
                if cancelled:
                    context.cancelled.set()
                return {"status": "success", "summary": "Premature"}

        sent, finished = [], asyncio.Event()

        async def write(frame):
            event = decode_line(frame)
            sent.append(event)
            if event.get("event") == "run.finished":
                finished.set()

        async def lines():
            yield encode_line(command("start", "start", {"run": FIXTURE["spec"]}))
            await finished.wait()
            yield encode_line(command("shutdown", "shutdown"))

        await serve(Premature(), lines(), write)
        terminal = next(event for event in sent if event.get("event") == "run.finished")
        assert terminal["data"]["status"] == ("cancelled" if cancelled else "failed")
        if not cancelled:
            assert terminal["data"]["error"]["code"] == "CONVERSATION_NOT_CLOSED"
    asyncio.run(scenario())


class Session:
    async def open(self, language, spec=None):
        argv = (
            [sys.executable, str(ROOT / "examples/fake/connector.py")]
            if language == "python"
            else ["node", str(ROOT / "examples/fake/connector.mjs")]
        )
        self.client = await ConnectorClient.launch(argv)
        self.queue, self.events = asyncio.Queue(), []

        async def collect():
            async for event in self.client.events():
                self.events.append(event)
                self.queue.put_nowait(event)

        self.collecting = asyncio.create_task(collect())
        await self.client.request("start", {"run": spec or FIXTURE["spec"]})
        await self.event("session.waiting_input")
        return self

    async def event(self, kind):
        async with asyncio.timeout(5):
            while True:
                value = await self.queue.get()
                if value["event"] == kind:
                    return value

    async def control(self, name, payload):
        return await self.client.request(
            "control",
            {"attempt_id": FIXTURE["spec"]["attempt_id"], "command": name, "payload": payload},
        )

    async def close(self):
        await self.client.close()
        await self.collecting


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_idle_close_is_possible_when_the_input_ledger_is_full(language):
    async def scenario():
        spec = copy.deepcopy(FIXTURE["spec"])
        spec["interaction"]["max_inputs"] = 1
        session = await Session().open(language, spec)
        try:
            await session.control("turn.submit", FIXTURE["turn"])
            await session.event("turn.completed")
            await session.control(
                "session.close", {"input_id": "close", "expected_revision": 1, "reason": "finished"}
            )
            assert (await session.event("run.finished"))["data"]["status"] == "success"
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_two_inputs_keep_one_execution_and_deduplicate_inputs(language):
    async def scenario():
        session = await Session().open(language)
        try:
            first = copy.deepcopy(FIXTURE["turn"])
            receipt = await session.control("turn.submit", first)
            await session.event("turn.completed")
            assert not any(event["event"] == "run.finished" for event in session.events)
            assert await session.control("turn.submit", first) == receipt
            conflict = {**first, "messages": [{"role": "user", "content": "changed"}]}
            with pytest.raises(ProtocolError) as caught:
                await session.control("turn.submit", conflict)
            assert caught.value.code == "INPUT_CONFLICT"
            second = {
                "input_id": "input-2",
                "turn_id": "turn-2",
                "expected_revision": 1,
                "messages": [{"role": "user", "content": "second"}],
            }
            await session.control("turn.submit", second)
            await session.event("turn.completed")
            await session.control(
                "session.close",
                {"input_id": "close", "expected_revision": 2, "reason": "user_stop"},
            )
            terminal = await session.event("run.finished")
            assert terminal["data"]["status"] == "success"
            assert terminal["data"]["summary"] == "Echo: second"
            assert len([event for event in session.events if event["event"] == "turn.started"]) == 2
            assert [event["seq"] for event in session.events] == list(range(len(session.events)))
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_external_tools_are_correlated_and_partial_results_are_atomic(language):
    async def scenario():
        session = await Session().open(language)
        try:
            turn = copy.deepcopy(FIXTURE["turn"])
            turn["messages"][0]["content"] = "tools"
            await session.control("turn.submit", turn)
            request = (await session.event("tool.request"))["data"]
            assert [call["call_id"] for call in request["calls"]] == ["first", "second"]
            base = {"turn_id": "turn-1", "batch_id": "batch-turn-1", "expected_revision": 1}
            second = {
                **base,
                "input_id": "second-result",
                "results": [{"call_id": "second", "content": "two", "is_error": False}],
            }
            await session.control("tool.respond", second)
            await session.control("tool.respond", second)
            conflict = {
                **second,
                "input_id": "conflicting-result",
                "results": [{"call_id": "second", "content": "changed", "is_error": False}],
            }
            with pytest.raises(ProtocolError) as caught:
                await session.control("tool.respond", conflict)
            assert caught.value.code == "TOOL_RESULT_CONFLICT"
            await session.control(
                "tool.respond",
                {
                    **base,
                    "input_id": "first-result",
                    "results": [{"call_id": "first", "content": "one", "is_error": False}],
                },
            )
            await session.event("turn.completed")
            await session.control(
                "session.close",
                {"input_id": "close", "expected_revision": 1, "reason": "user_stop"},
            )
            terminal = await session.event("run.finished")
            assert json.loads(terminal["data"]["summary"]) == ["one", "two"]
        finally:
            await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["python", "typescript"])
@pytest.mark.parametrize("phase", ["waiting_input", "generating", "waiting_tool"])
def test_abort_during_each_conversation_boundary(language, phase):
    async def scenario():
        session = await Session().open(language)
        try:
            if phase != "waiting_input":
                turn = copy.deepcopy(FIXTURE["turn"])
                turn["messages"][0]["content"] = "tools" if phase == "waiting_tool" else "wait"
                await session.control("turn.submit", turn)
                await session.event("tool.request" if phase == "waiting_tool" else "turn.started")
            await session.control("abort", {})
            assert (await session.event("run.finished"))["data"]["status"] == "cancelled"
        finally:
            await session.close()

    asyncio.run(scenario())
