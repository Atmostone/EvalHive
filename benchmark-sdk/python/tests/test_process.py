import asyncio
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from evalhive_benchmark import ProtocolError
from evalhive_benchmark.client import ConnectorClient

ROOT = Path(__file__).parents[2]
FIXTURE = json.loads((ROOT / "fixtures/conformance.json").read_text())


def argv(language):
    directory = ROOT / "examples/fake"
    return (
        [sys.executable, str(directory / "connector.py")]
        if language == "python"
        else ["node", str(directory / "connector.mjs")]
    )


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_external_package_lifecycle_and_broker(language):
    async def scenario():
        allocations, events = [], []

        async def broker(request):
            assert request["scope"] == FIXTURE["spec"]["scope"]
            allocations.append(request)
            return {"handle": "environment-example"}

        client = await ConnectorClient.launch(argv(language), broker=broker)

        async def collect():
            async for event in client.events():
                events.append(event)

        reading = asyncio.create_task(collect())
        try:
            assert (await client.request("describe"))["id"] == "example.benchmark"
            assert (await client.request("list_cases", {"dataset_id": "example", "revision": "1"}))[
                "cases"
            ][0] == FIXTURE["case"]
            assert (
                await client.request("resolve_case", {"case": FIXTURE["case"]["ref"]})
                == FIXTURE["case"]
            )
            assert (await client.request("validate", {"spec": FIXTURE["spec"]}))["accepted"]
            plan = await client.request("plan", {"spec": FIXTURE["spec"]})
            params = {"spec": FIXTURE["spec"], "plan": plan}
            first = await client.request("prepare", params, request_id="prepare-1")
            assert first["ready"] is True
            assert await client.request("prepare", params, request_id="prepare-1") == first
            assert len(allocations) == 1
            changed = copy.deepcopy(params)
            changed["spec"]["config"]["prefix"] = "changed"
            with pytest.raises(ProtocolError) as caught:
                await client.request("prepare", changed, request_id="prepare-1")
            assert caught.value.code == "COMMAND_CONFLICT"
            scoped = {"scope": FIXTURE["spec"]["scope"], "payload": {}}
            assert (await client.request("drive", scoped))["finished"]
            assert (await client.request("capture", scoped))["regrade"] == "available"
            assert (await client.request("evaluate", scoped))["success"]
            assert (await client.request("aggregate", {"payload": {}}))["observed"] == 1
            assert (await client.request("cleanup", scoped))["cleaned"]
            foreign = {"scope": {**scoped["scope"], "generation": 2}, "payload": {}}
            with pytest.raises(ProtocolError) as caught:
                await client.request("cleanup", foreign)
            assert caught.value.code == "SCOPE_CONFLICT"
        finally:
            await client.close()
            await reading
        assert [event["seq"] for event in events] == [0, 1]
        assert [event["event"] for event in events] == ["environment.ready", "agent.request"]

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_cancellation_while_prepare_is_pending(language):
    async def scenario():
        client = await ConnectorClient.launch(argv(language))
        spec = copy.deepcopy(FIXTURE["spec"])
        spec["config"]["wait"] = True
        pending = asyncio.create_task(
            client.request("prepare", {"spec": spec, "plan": FIXTURE["plan"]})
        )
        try:
            # Independent RPC must remain responsive while native preparation waits.
            assert (await client.request("describe"))["id"] == "example.benchmark"
            assert (await client.request("cancel", {"scope": spec["scope"], "payload": {}}))[
                "cancelled"
            ]
            with pytest.raises(ProtocolError) as caught:
                await pending
            assert caught.value.code == "CANCELLED"
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_broker_errors_are_propagated_without_native_side_effect_retry(language):
    async def scenario():
        async def broker(request):
            raise ProtocolError("RESOURCE_DENIED", "Fixture resource denied")

        client = await ConnectorClient.launch(argv(language), broker=broker)
        try:
            params = {"spec": FIXTURE["spec"], "plan": FIXTURE["plan"]}
            for _ in range(2):
                with pytest.raises(ProtocolError) as caught:
                    await client.request("prepare", params, request_id="denied-prepare")
                assert caught.value.code == "RESOURCE_DENIED"
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_full_wire_receipts_preserve_remote_errors_for_durable_storage(language):
    async def scenario():
        async def denied(request):
            raise ProtocolError("REMOTE_UNCERTAIN", "Private fixture error", "uncertain")

        client = await ConnectorClient.launch(argv(language), broker=denied)
        try:
            receipt = await client.request_receipt("describe", request_id="full-description")
            assert receipt["type"] == "response" and receipt["id"] == "full-description"
            assert receipt["ok"] and receipt["result"]["kind"] == "benchmark"
            error = await client.request_receipt(
                "prepare",
                {"spec": FIXTURE["spec"], "plan": FIXTURE["plan"]},
                request_id="full-error",
            )
            assert not error["ok"] and error["error"]["kind"] == "uncertain"
            assert error["error"]["code"] == "REMOTE_UNCERTAIN"
            with pytest.raises(ProtocolError) as caught:
                await client.request(
                    "prepare",
                    {"spec": FIXTURE["spec"], "plan": FIXTURE["plan"]},
                    request_id="full-error",
                )
            assert caught.value.code == "REMOTE_UNCERTAIN"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_close_detaches_when_native_wait_and_stream_do_not_finish():
    async def scenario():
        released, signals = asyncio.Event(), []

        async def native_wait():
            # A fenced native operation may finish after the caller cancels it.
            try:
                await released.wait()
            except asyncio.CancelledError:
                await released.wait()

        process = SimpleNamespace(
            stdin=SimpleNamespace(close=lambda: None),
            stdout=asyncio.StreamReader(),
            wait=native_wait,
            terminate=lambda: signals.append("terminate"),
            kill=lambda: signals.append("kill"),
        )
        client = ConnectorClient(process)
        try:
            with pytest.raises(ProtocolError) as caught:
                await asyncio.wait_for(client.close(0.02, shutdown=False), 1)
            assert caught.value.code == "TRANSPORT_CLOSE_INCOMPLETE"
            assert signals == ["terminate", "kill"]
            await asyncio.sleep(0)
            assert client.reading.done()
            with pytest.raises(ProtocolError, match="detached"):
                async for _ in client.events():
                    pytest.fail("No fixture events were sent")
        finally:
            released.set()
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_close_detaches_if_process_exits_but_inherited_stdout_stays_open():
    async def scenario():
        async def exited():
            return 0

        process = SimpleNamespace(
            stdin=SimpleNamespace(close=lambda: None),
            stdout=asyncio.StreamReader(),
            wait=exited,
            terminate=lambda: pytest.fail("An exited process must not be signalled"),
            kill=lambda: pytest.fail("An exited process must not be signalled"),
        )
        client = ConnectorClient(process)
        with pytest.raises(ProtocolError) as caught:
            await asyncio.wait_for(client.close(0.02, shutdown=False), 1)
        assert caught.value.code == "TRANSPORT_CLOSE_INCOMPLETE"
        await asyncio.sleep(0)
        assert client.reading.done()

    asyncio.run(scenario())


def test_closing_an_already_cancelled_reader_does_not_cancel_the_caller():
    async def scenario():
        async def exited():
            return 0

        process = SimpleNamespace(
            stdin=SimpleNamespace(close=lambda: None),
            stdout=asyncio.StreamReader(),
            wait=exited,
            terminate=lambda: None,
            kill=lambda: None,
        )
        client = ConnectorClient(process)
        client.reading.cancel()
        await asyncio.gather(client.reading, return_exceptions=True)
        with pytest.raises(ProtocolError) as incomplete:
            await client.close(0.02, shutdown=False)
        assert incomplete.value.code == "TRANSPORT_CLOSE_INCOMPLETE"
        assert asyncio.current_task().cancelling() == 0

    asyncio.run(scenario())
