import asyncio
import sys
from pathlib import Path

import pytest
from evalhive_benchmark import ConnectorClient, ProtocolError

ROOT = Path(__file__).parents[2]
SCOPE = {"workspace_id": "workspace", "trial_id": "trial", "generation": 1}


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_provider_package_operations_and_scope_fencing(language):
    async def scenario():
        entry = ROOT / "examples/fake" / ("provider.py" if language == "python" else "provider.mjs")
        client = await ConnectorClient.launch([sys.executable if language == "python" else "node", str(entry)])
        events = []

        async def collect():
            async for event in client.events():
                events.append(event)

        reading = asyncio.create_task(collect())
        try:
            assert (await client.request("describe"))["kind"] == "environment_provider"
            def call(operation, payload=None, **kwargs):
                return client.resource(SCOPE, "example.provider", operation, payload or {}, **kwargs)
            allocated = await call("allocate", {"kind": "custom.fixture"}, request_id="allocation")
            assert await call("allocate", {"kind": "custom.fixture"}, request_id="allocation") == allocated
            with pytest.raises(ProtocolError, match="different content"):
                await call("allocate", {"kind": "changed"}, request_id="allocation")
            handle = allocated["handle"]
            assert (await call("inspect", handle=handle))["allocated"] == 1
            for provider, scope, operation, target, code in [
                ("other.provider", SCOPE, "inspect", handle, "PROVIDER_MISMATCH"),
                ("example.provider", {**SCOPE, "generation": 2}, "inspect", handle, "SCOPE_CONFLICT"),
                ("example.provider", SCOPE, "inspect", "foreign", "FOREIGN_HANDLE"),
                ("example.provider", SCOPE, "export", handle, "UNSUPPORTED_CAPABILITY"),
            ]:
                with pytest.raises(ProtocolError) as caught:
                    await client.resource(scope, provider, operation, {}, handle=target)
                assert caught.value.code == code
            with pytest.raises(ProtocolError) as caught:
                await call("exec", {"fail": True}, handle=handle)
            assert caught.value.code == "CONNECTOR_ERROR"
            assert "private-native" not in str(caught.value)
            pending = asyncio.create_task(call("exec", {"wait": True}, handle=handle))
            await asyncio.sleep(0)
            assert (await client.request("cancel", {"scope": SCOPE, "payload": {}}))["accepted"]
            with pytest.raises(ProtocolError) as caught:
                await pending
            assert caught.value.code == "CANCELLED"
            with pytest.raises(ProtocolError, match="cancelled"):
                await call("allocate")
            assert (await call("stop", handle=handle))["stopped"]
            assert (await call("release", handle=handle))["released"]
            await client.peer.acknowledge("broker_request", "allocation")
            with pytest.raises(ProtocolError) as caught:
                await call("allocate", {"kind": "custom.fixture"}, request_id="allocation")
            assert caught.value.code == "RECEIPT_RETIRED"
        finally:
            await client.close()
            await reading
        assert len(events) == 1 and events[0]["source"] == "provider"

    asyncio.run(scenario())
