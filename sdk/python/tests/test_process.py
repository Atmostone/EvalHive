import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest
from evalhive_harness.client import ConnectorClient
from evalhive_harness.protocol import ProtocolError

SDK = Path(__file__).parents[2]
FIXTURES = json.loads((SDK / "fixtures/conformance.json").read_text())
EXAMPLE = SDK / "examples/fake"


async def collect_until_finished(client):
    events = []
    async for event in client.events():
        events.append(event)
        if event["event"] == "run.finished":
            break
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert len({e["event_id"] for e in events}) == len(events)
    assert events[0]["event"] == "run.started"
    return events


@pytest.mark.parametrize("language", ["python", "typescript"])
@pytest.mark.parametrize("mode", ["success", "failure", "abort", "shutdown"])
def test_real_processes_and_control(language, mode):
    async def scenario():
        argv = (
            [sys.executable, str(EXAMPLE / "connector.py")]
            if language == "python"
            else ["node", str(EXAMPLE / "connector.mjs")]
        )
        client = await ConnectorClient.launch(argv)
        run = copy.deepcopy(FIXTURES["run_spec"])
        run["config"].update({"wait": mode in ("abort", "shutdown"), "fail": mode == "failure"})
        try:
            assert (await client.request("describe"))["id"] == "example.fake"
            assert (await client.request("validate", {"run": run}))["accepted"]
            assert (await client.request("start", {"run": run}))["accepted"]
            if mode in ("abort", "shutdown"):
                with pytest.raises(ProtocolError) as exc:
                    await client.request("control", {"attempt_id": "old", "command": "abort"})
                assert exc.value.code == "ATTEMPT_NOT_ACTIVE"
                with pytest.raises(ProtocolError) as exc:
                    await client.request(
                        "control", {"attempt_id": run["attempt_id"], "command": "approval"}
                    )
                assert exc.value.code == "UNSUPPORTED_CAPABILITY"
                with pytest.raises(ProtocolError) as exc:
                    await client.request(
                        "control",
                        {
                            "attempt_id": run["attempt_id"],
                            "command": "feedback",
                            "payload": {"text": 2},
                        },
                    )
                assert exc.value.code == "INVALID_CONFIG"
                await client.request(
                    "control",
                    {
                        "attempt_id": run["attempt_id"],
                        "command": "feedback",
                        "payload": {"text": "Continue"},
                    },
                )
            if mode == "abort":
                await client.request(
                    "control", {"attempt_id": run["attempt_id"], "command": "abort"}
                )
            if mode == "shutdown":
                await client.close()
            events = await asyncio.wait_for(collect_until_finished(client), 10)
            result = events[-1]["data"]
            assert (
                result["status"]
                == {
                    "success": "success",
                    "failure": "failed",
                    "abort": "cancelled",
                    "shutdown": "cancelled",
                }[mode]
            )
            assert "Internal diagnostics" not in json.dumps(events)
            if mode == "success":
                assert any(e["event"] == "artifact" for e in events)
                assert any("Hello" in str(e["data"]) for e in events)
            if mode != "shutdown":
                with pytest.raises(ProtocolError) as exc:
                    await client.request("start", {"run": run})
                assert exc.value.code == "ALREADY_STARTED"
        finally:
            await client.close()
        assert client.process.returncode == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("output,error", [("{}\\n", "INVALID_FRAME"), ("", "TRANSPORT_CLOSED")])
def test_broken_stream_fails_pending_request_and_event_consumer(output, error):
    async def scenario():
        code = f"import sys; sys.stdin.readline(); sys.stdout.write({output!r}); sys.stdout.flush()"
        client = await ConnectorClient.launch([sys.executable, "-c", code])
        try:
            with pytest.raises(ProtocolError) as exc:
                await client.request("describe", timeout=2)
            assert exc.value.code == error
            with pytest.raises(ProtocolError) as exc:
                async for _ in client.events():
                    pass
            assert exc.value.code == error
        finally:
            await client.close(grace=0.2)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["reorder", "late", "flood", "hang"])
def test_adversarial_wire_peer(mode):
    async def scenario():
        client = await ConnectorClient.launch(
            [sys.executable, str(SDK / "fixtures/peer.py"), mode],
            event_buffer=1,
            max_pending=1 if mode == "hang" else 64,
        )
        try:
            if mode == "reorder":
                first, second = await asyncio.gather(
                    client.request("shutdown"), client.request("describe")
                )
                assert first == {"method": "shutdown"}
                assert second["id"] == "example.fake"
            elif mode == "late":
                with pytest.raises(ProtocolError) as exc:
                    await client.request("shutdown", timeout=0.05)
                assert exc.value.code == "TIMEOUT"
                assert (await client.request("describe"))["id"] == "example.fake"
            elif mode == "flood":
                with pytest.raises(ProtocolError) as exc:
                    await client.request("describe")
                assert exc.value.code == "EVENT_BUFFER_FULL"
                received = []
                with pytest.raises(ProtocolError) as exc:
                    async for event in client.events():
                        received.append(event)
                assert exc.value.code == "EVENT_BUFFER_FULL"
                assert len(received) == 1
            else:
                pending = asyncio.create_task(client.request("describe", timeout=0.2))
                await asyncio.sleep(0)
                with pytest.raises(ProtocolError) as exc:
                    await client.request("describe")
                assert exc.value.code == "PENDING_LIMIT"
                with pytest.raises(ProtocolError) as exc:
                    await pending
                assert exc.value.code == "TIMEOUT"
        finally:
            await client.close(grace=0.1)

    asyncio.run(scenario())
