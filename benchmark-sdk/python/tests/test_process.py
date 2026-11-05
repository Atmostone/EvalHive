import asyncio
import copy
import json
import sys
from pathlib import Path

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
