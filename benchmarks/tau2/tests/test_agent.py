"""External steps preserve native causal identities without executing tools."""

import asyncio
import time
from copy import deepcopy

import pytest
from evalhive_benchmark import ProtocolError
from evalhive_tau2.agent import ExternalSession


class Harness:
    def __init__(self):
        self.cancelled = asyncio.Event()
        self.events, self.commands = [], []
        self.ids = set()
        self.mapping = [
            {
                "native_name": "calculate",
                "exposed_name": "chosen_calculate",
                "schema_sha256": "a" * 64,
            }
        ]
        self.results = None
        self.initial_history = [{"role": "assistant", "content": "Hello"}]

    def emit(self, event, data):
        self.events.append({"event": event, "data": deepcopy(data)})

    async def agent(self, scope, operation, payload, *, session_id, request_id, timeout):
        assert 0 < timeout <= 30 and scope == {"trial_id": "trial"}
        assert request_id not in self.ids
        self.ids.add(request_id)
        if operation == "open":
            assert session_id is None
            assert payload["initial_history"] == self.initial_history
            self.emit(
                "native", {"name": "external_tools.mapping", "payload": {"tools": self.mapping}}
            )
            self.emit("session.waiting_input", {"revision": 0})
            return {"session_id": "session"}
        assert session_id == "session"
        if operation == "events":
            # One-frame pages prove that output/request boundaries need not be adjacent.
            cursor = payload["cursor"]
            return {
                "events": self.events[cursor : cursor + 1],
                "cursor": min(cursor + 1, len(self.events)),
                "closed": False,
            }
        if operation == "close":
            return {"status": "success", "outcome": {"attempt_id": "session"}}
        assert operation == "control"
        self.commands.append(deepcopy(payload))
        command = payload["payload"]
        assert command["expected_revision"] in (0, 1)
        if payload["name"] == "turn.submit":
            calls = [
                {
                    "call_id": identity,
                    "name": "chosen_calculate",
                    "arguments": {"expression": expression},
                }
                for identity, expression in [("a", "2+2"), ("b", "3+3")]
            ]
            self.emit("turn.output", {"output": {"content": "", "tool_calls": calls}})
            self.emit("usage", {"input_tokens": 5})
            self.emit(
                "tool.request",
                {"revision": 1, "turn_id": command["turn_id"], "batch_id": "batch", "calls": calls},
            )
        else:
            assert payload["name"] == "tool.respond"
            self.results = command["results"]
            self.emit("turn.output", {"output": {"content": "Done"}})
            self.emit("turn.completed", {"revision": 1})
            self.emit("session.waiting_input", {"revision": 1})
        return {"result": {"ok": True, "result": {"accepted": True, "revision": 1}}}


def session(harness):
    return ExternalSession(
        harness,
        {"trial_id": "trial"},
        deadline=time.monotonic() + 30,
        schemas={"calculate": "a" * 64},
    )


@pytest.mark.asyncio
async def test_parallel_results_are_bound_by_native_call_id_across_event_pages():
    harness = Harness()
    driver = session(harness)
    history = [{"role": "assistant", "content": "Hello"}]
    output = await driver.exchange({"role": "user", "content": "Calculate"}, history)
    assert [(call["id"], call["name"]) for call in output["tool_calls"]] == [
        ("a", "calculate"),
        ("b", "calculate"),
    ]
    results = [
        {"call_id": "b", "content": "6", "is_error": False},
        {"call_id": "a", "content": "4", "is_error": True},
    ]
    assert await driver.exchange({"role": "tool", "results": results}, []) == {
        "content": "Done",
        "tool_calls": None,
    }
    assert harness.results == results and driver.waiting
    assert (await driver.close())["status"] == "success"
    assert len(harness.commands) == 2


@pytest.mark.asyncio
async def test_unknown_mapping_and_missing_tool_results_fail_before_continuation():
    harness = Harness()
    harness.mapping[0]["schema_sha256"] = "b" * 64
    with pytest.raises(ProtocolError) as changed:
        await session(harness).open([{"role": "assistant", "content": "Hello"}])
    assert changed.value.code == "NATIVE_TOOL_MAPPING" and harness.commands == []
    harness = Harness()
    driver = session(harness)
    await driver.exchange(
        {"role": "user", "content": "Calculate"}, [{"role": "assistant", "content": "Hello"}]
    )
    with pytest.raises(ProtocolError) as missing:
        await driver.exchange(
            {
                "role": "tool",
                "results": [{"call_id": "foreign", "content": "4", "is_error": False}],
            },
            [],
        )
    assert missing.value.code == "NATIVE_TOOL_RESULTS" and len(harness.commands) == 1


@pytest.mark.asyncio
async def test_cancelled_participant_never_opens_a_harness():
    harness = Harness()
    harness.cancelled.set()
    with pytest.raises(ProtocolError) as cancelled:
        await session(harness).open([])
    assert cancelled.value.code == "NATIVE_AGENT_STOPPED" and not harness.ids


@pytest.mark.asyncio
async def test_initial_idle_and_tool_mapping_can_arrive_in_either_order():
    class EarlyIdleHarness(Harness):
        async def agent(self, scope, operation, payload, **options):
            result = await super().agent(scope, operation, payload, **options)
            if operation == "open":
                self.events.reverse()
            return result

    harness = EarlyIdleHarness()
    driver = session(harness)
    await driver.open(harness.initial_history)
    assert driver.waiting and driver.mapping == {"chosen_calculate": "calculate"}
    assert not harness.commands


@pytest.mark.asyncio
async def test_native_step_limit_aborts_a_pending_batch_before_confirming_close():
    class StoppableHarness(Harness):
        stopped = False

        async def agent(self, scope, operation, payload, **options):
            if operation == "control" and payload["name"] == "abort":
                self.stopped = True
                return {"result": {"ok": True}}
            if operation == "events" and self.stopped:
                return {"events": [], "cursor": payload["cursor"], "closed": True}
            return await super().agent(scope, operation, payload, **options)

    harness = StoppableHarness()
    driver = session(harness)
    await driver.exchange(
        {"role": "user", "content": "Calculate"}, [{"role": "assistant", "content": "Hello"}]
    )
    assert driver.pending is not None
    assert (await driver.close())["status"] == "success"
    assert harness.stopped
