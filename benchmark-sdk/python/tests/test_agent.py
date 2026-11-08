"""External benchmark packages use the same scoped host agent API in both SDKs."""

import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest
from evalhive_benchmark import ConnectorClient, ProtocolError, command
from evalhive_benchmark.peer import Peer

ROOT = Path(__file__).parents[2]
FIXTURE = json.loads((ROOT / "fixtures/conformance.json").read_text())
FIXTURE["spec"]["case"]["requirements"].setdefault("capabilities", []).append("host.agent_api_v1")


def argv(language):
    root = ROOT / "examples/fake"
    return (
        [sys.executable, str(root / "connector.py")]
        if language == "python"
        else ["node", str(root / "connector.mjs")]
    )


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_agent_calls_share_the_transport_with_resources_and_read_retired_receipts(language):
    async def scenario():
        calls, replays, resources = [], [], []
        answer = {
            "session_id": "session-one",
            "status": "failed",
            "outcome": {"attempt_id": "attempt-one", "evidence_digest": "a" * 64},
        }

        async def agent(request):
            assert (
                request["method"] == "agent.call"
                and request["params"]["scope"] == FIXTURE["spec"]["scope"]
            )
            calls.append(request)
            return answer

        async def replay(request):
            replays.append(request)
            return answer

        async def broker(request):
            resources.append(request)
            return {"handle": "prepared-environment"}

        client = await ConnectorClient.launch(
            argv(language), agent=agent, agent_replay=replay, broker=broker
        )
        try:
            await client.request("prepare", {"spec": FIXTURE["spec"], "plan": FIXTURE["plan"]})
            reply = await client.request(
                "drive",
                {
                    "scope": FIXTURE["spec"]["scope"],
                    "payload": {"agent_operation": "run", "repeat_agent": True},
                },
            )
            assert reply["agent"] == answer
            assert len(calls) == len(replays) == len(resources) == 1
            assert (await client.request("describe"))["id"] == "example.benchmark"
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_agent_receipts_do_not_repeat_effects_when_replay_is_unavailable(language):
    async def scenario():
        calls = []

        async def agent(request):
            calls.append(request)
            return {
                "session_id": "session-one",
                "status": "success",
                "outcome": {"attempt_id": "attempt-one", "evidence_digest": "a" * 64},
            }

        client = await ConnectorClient.launch(argv(language), agent=agent)
        try:
            await client.request("validate", {"spec": FIXTURE["spec"]})
            with pytest.raises(ProtocolError) as caught:
                await client.request(
                    "drive",
                    {
                        "scope": FIXTURE["spec"]["scope"],
                        "payload": {"agent_operation": "run", "repeat_agent": True},
                    },
                )
            assert caught.value.code == "RECEIPT_UNAVAILABLE" and caught.value.kind == "uncertain"
            assert len(calls) == 1
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_cancellation_stops_a_worker_waiting_for_its_agent(language):
    async def scenario():
        entered = asyncio.Event()

        async def agent(request):
            entered.set()
            await asyncio.Event().wait()

        client = await ConnectorClient.launch(argv(language), agent=agent)
        try:
            await client.request("validate", {"spec": FIXTURE["spec"]})
            scope = FIXTURE["spec"]["scope"]
            pending = asyncio.create_task(
                client.request("drive", {"scope": scope, "payload": {"agent_operation": "run"}})
            )
            await asyncio.wait_for(entered.wait(), 5)
            await client.request("cancel", {"scope": scope, "payload": {}})
            with pytest.raises(ProtocolError) as caught:
                await asyncio.wait_for(pending, 3)
            assert caught.value.code == "CANCELLED"
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_evaluation_workers_cannot_call_agents_and_host_cannot_send_reverse_calls(language):
    async def scenario():
        calls = []

        async def agent(request):
            calls.append(request)
            return {}

        client = await ConnectorClient.launch(argv(language), agent=agent)
        try:
            spec = FIXTURE["evaluation_spec"]
            await client.request("validate", {"spec": spec})
            with pytest.raises(ProtocolError) as forbidden:
                await client.request(
                    "evaluate", {"scope": spec["scope"], "payload": {"agent_operation": "run"}}
                )
            assert forbidden.value.code == "AGENT_UNAVAILABLE" and calls == []
            with pytest.raises(ProtocolError) as wrong:
                await client.request(
                    "agent.call", {"scope": spec["scope"], "operation": "run", "payload": {}}
                )
            assert wrong.value.code == "WRONG_DIRECTION"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_request_deadline_includes_a_blocked_transport_write():
    async def scenario():
        async def blocked(frame):
            await asyncio.Event().wait()

        peer = Peer(blocked)
        with pytest.raises(ProtocolError) as caught:
            await asyncio.wait_for(peer.request(command("blocked", "shutdown"), timeout=0.02), 1)
        assert caught.value.code == "COMMAND_UNCERTAIN" and peer.pending == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_agent_session_controls_events_and_close_keep_their_session(language):
    async def scenario():
        answers = {
            "open": {"session_id": "session-one", "attempt_id": "attempt-one"},
            "control": {
                "session_id": "session-one",
                "command_id": "command-one",
                "result": {"accepted": True},
            },
            "events": {
                "session_id": "session-one",
                "events": [{"event": "turn.completed"}],
                "cursor": 1,
                "closed": False,
            },
            "close": {
                "session_id": "session-one",
                "status": "success",
                "outcome": {"attempt_id": "attempt-one", "evidence_digest": "a" * 64},
            },
        }

        async def agent(request):
            operation = request["params"]["operation"]
            if operation != "open":
                assert request["params"]["session_id"] == "session-one"
            return answers[operation]

        client = await ConnectorClient.launch(argv(language), agent=agent)
        try:
            await client.request("validate", {"spec": FIXTURE["spec"]})
            for operation, answer in answers.items():
                payload = {"agent_operation": operation, "agent_request_id": "request-" + operation}
                if operation != "open":
                    payload["agent_session"] = "session-one"
                if operation == "control":
                    payload["agent_payload"] = {
                        "name": "turn.submit",
                        "payload": {"text": "Fixture"},
                    }
                if operation == "events":
                    payload["agent_payload"] = {"cursor": 0}
                result = await client.request(
                    "drive", {"scope": FIXTURE["spec"]["scope"], "payload": payload}
                )
                assert result["agent"] == answer
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["python", "typescript"])
@pytest.mark.parametrize("wrong", ["shape", "session", "cursor"])
def test_invalid_agent_responses_are_recording_errors(language, wrong):
    async def scenario():
        async def agent(request):
            return (
                {"finished": True}
                if wrong == "shape"
                else {
                    "session_id": "other-session" if wrong == "session" else "session-one",
                    "events": [],
                    "cursor": 0,
                    "closed": False,
                }
            )

        client = await ConnectorClient.launch(argv(language), agent=agent)
        try:
            await client.request("validate", {"spec": FIXTURE["spec"]})
            with pytest.raises(ProtocolError) as caught:
                await client.request(
                    "drive",
                    {
                        "scope": FIXTURE["spec"]["scope"],
                        "payload": {
                            "agent_operation": "events",
                            "agent_session": "session-one",
                            "agent_payload": {"cursor": 1 if wrong == "cursor" else 0},
                        },
                    },
                )
            assert caught.value.code == "AGENT_REPLY_INVALID" and caught.value.kind == "recording"
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_agent_capability_must_be_declared_before_calling_the_host(language):
    async def scenario():
        calls = []

        async def agent(request):
            calls.append(request)
            return {}

        spec = copy.deepcopy(FIXTURE["spec"])
        spec["case"]["requirements"]["capabilities"].remove("host.agent_api_v1")
        client = await ConnectorClient.launch(argv(language), agent=agent)
        try:
            await client.request("validate", {"spec": spec})
            with pytest.raises(ProtocolError) as caught:
                await client.request(
                    "drive", {"scope": spec["scope"], "payload": {"agent_operation": "run"}}
                )
            assert caught.value.code == "HOST_CAPABILITY_REQUIRED" and calls == []
        finally:
            await client.close()

    asyncio.run(scenario())
