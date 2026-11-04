"""Exercise actual socket framing without requiring another Docker daemon."""

import asyncio
import json
import socket
from types import SimpleNamespace

import pytest
from app.harnesses.catalog import get_manifest
from app.runtime.attach import DockerAttachedProcess
from evalhive_harness import ProtocolError, encode_line, response
from evalhive_harness.client import ConnectorClient


def frame(stream, data):
    return bytes([stream, 0, 0, 0]) + len(data).to_bytes(4, "big") + data


@pytest.mark.asyncio
async def test_sdk_commands_and_unicode_cross_multiplexed_docker_socket():
    local, remote = socket.socketpair()
    remote.setblocking(False)
    state = {"status": "running", "exit_code": None}
    runtime = SimpleNamespace(inspect=lambda *_: state)
    process = DockerAttachedProcess(runtime, "container", "workspace", local)
    client = ConnectorClient(process)
    manifest = {**get_manifest("pi"), "display_name": "Pi 🌍"}

    async def peer():
        buffer = bytearray()
        try:
            while True:
                while b"\n" not in buffer:
                    data = await asyncio.get_running_loop().sock_recv(remote, 4096)
                    if not data:
                        return
                    buffer.extend(data)
                index = buffer.index(10)
                command = json.loads(buffer[:index])
                del buffer[: index + 1]
                result = manifest if command["method"] == "describe" else {"stopped": True}
                data = encode_line(response(command["id"], result))
                half = len(data) // 2
                await asyncio.get_running_loop().sock_sendall(remote, frame(2, b"diagnostic"))
                await asyncio.get_running_loop().sock_sendall(remote, frame(1, data[:half]))
                await asyncio.sleep(0)
                await asyncio.get_running_loop().sock_sendall(remote, frame(1, data[half:]))
                if command["method"] == "shutdown":
                    return
        finally:
            state.update(status="exited", exit_code=0)
            remote.close()

    server = asyncio.create_task(peer())
    try:
        assert await client.request("describe", timeout=2) == manifest
        await client.close(grace=0.2)
        assert process.returncode == 0 and bytes(process.stderr) == b"diagnosticdiagnostic"
    finally:
        await process.aclose()
        await asyncio.gather(server, return_exceptions=True)


@pytest.mark.asyncio
async def test_truncated_docker_frame_fails_pending_sdk_request():
    local, remote = socket.socketpair()
    remote.setblocking(False)
    runtime = SimpleNamespace(inspect=lambda *_: {"status": "exited", "exit_code": 1})
    process = DockerAttachedProcess(runtime, "container", "workspace", local)
    client = ConnectorClient(process)
    await asyncio.get_running_loop().sock_sendall(remote, bytes([1, 0, 0, 0, 0, 0, 0, 10]) + b"{")
    remote.close()
    try:
        with pytest.raises(ProtocolError):
            await client.request("describe", timeout=2)
    finally:
        await client.close(grace=0.1)
        await process.aclose()
