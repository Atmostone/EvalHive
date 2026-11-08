"""A stale controller detaches its socket without assuming native cleanup."""

import asyncio
import socket
import uuid
from types import SimpleNamespace

import pytest
from app.runtime.managed_attach import ManagedDockerProcess
from app.runtime.managed_spec import OwnerScope
from evalhive_benchmark import ProtocolError
from evalhive_benchmark.client import ConnectorClient


def test_fenced_worker_stop_does_not_hang_or_bypass_resource_authority():
    async def scenario():
        local, remote = socket.socketpair()
        scope = OwnerScope(*(str(uuid.uuid4()) for _ in range(3)), 1)
        handle = SimpleNamespace(scope=scope, container_id="owned-worker")
        native = {"status": "running", "exit_code": None}
        operations = []

        async def control(operation):
            operations.append(operation)
            raise ProtocolError("STALE_GENERATION", "The old controller is fenced")

        def forbidden(*args, **kwargs):
            pytest.fail("A stale transport must not bypass its authoritative control callback")

        runtime = SimpleNamespace(inspect=lambda *_: dict(native), stop=forbidden)
        process = ManagedDockerProcess(runtime, handle, local, control)
        client = ConnectorClient(process)
        try:
            with pytest.raises(ProtocolError) as caught:
                await asyncio.wait_for(client.close(0.02, shutdown=False), 2)
            assert caught.value.code == "TRANSPORT_CLOSE_INCOMPLETE"
            assert operations == ["stop", "stop"]
            assert native == {"status": "running", "exit_code": None}
            assert local.fileno() == -1
            assert process._pump.done()
            await asyncio.sleep(0)
            assert client.reading.done()
            assert all(task.done() for task in process._stop_tasks)
        finally:
            remote.close()
            await process.aclose()

    asyncio.run(scenario())
