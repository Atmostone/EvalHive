"""Adapt Docker's multiplexed attach socket to the SDK process-client interface.

The runner and its API token stay outside the connector container. Only the
connector's stdin/stdout cross this boundary; stderr is bounded diagnostics.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import suppress

from app.runtime.managed_exec import MAX_RECORD as MAX_LINE_BYTES


class DockerAttachedProcess:
    def __init__(self, runtime, container_id: str, workspace_id: str, connection):
        self.runtime, self.container_id, self.workspace_id = runtime, container_id, workspace_id
        self.connection = connection
        self.socket = getattr(connection, "_sock", connection)
        self.socket.setblocking(False)
        self.stdin, self.stdout = _Input(self), _Output()
        self.returncode = None
        self.stderr = bytearray()
        self.stderr_truncated = False
        self._pump = asyncio.create_task(self._read())
        self._stop_tasks = []

    @classmethod
    async def attach(cls, runtime, container_id: str, workspace_id: str):
        container = await asyncio.to_thread(runtime._owned, container_id, workspace_id)
        if container is None:
            raise ValueError("Connector container is unavailable in this workspace")
        connection = await asyncio.to_thread(
            container.attach_socket,
            params={
                "stdin": 1,
                "stdout": 1,
                "stderr": 1,
                "stream": 1,
                "logs": 0,
            },
        )
        return cls(runtime, container_id, workspace_id, connection)

    async def _exact(self, size):
        data = bytearray()
        while len(data) < size:
            chunk = await asyncio.get_running_loop().sock_recv(self.socket, size - len(data))
            if not chunk:
                if data:
                    raise OSError("Truncated Docker stream frame")
                return None
            data.extend(chunk)
        return bytes(data)

    async def _read(self):
        try:
            while header := await self._exact(8):
                size = int.from_bytes(header[4:], "big")
                if header[0] not in (1, 2) or header[1:4] != b"\0\0\0" or size > 2 * MAX_LINE_BYTES:
                    raise OSError("Invalid Docker stream frame")
                data = await self._exact(size)
                if data is None and size:
                    raise OSError("Truncated Docker stream frame")
                if header[0] == 1:
                    await self.stdout.queue.put(data or b"")
                else:
                    remaining = max(0, 65536 - len(self.stderr))
                    self.stderr.extend((data or b"")[:remaining])
                    self.stderr_truncated |= size > remaining
        except asyncio.CancelledError:
            return
        except Exception:
            self.stdout.error = OSError("Connector attach stream failed")
        finally:
            await self.stdout.queue.put(None)

    async def wait(self):
        while True:
            state = await asyncio.to_thread(
                self.runtime.inspect, self.container_id, self.workspace_id
            )
            if state is None or state["status"] in ("exited", "dead"):
                self.returncode = None if state is None else state["exit_code"]
                return self.returncode
            await asyncio.sleep(0.2)

    def terminate(self):
        self._stop_tasks.append(
            asyncio.create_task(
                asyncio.to_thread(
                    self.runtime.stop, self.container_id, self.workspace_id, grace_seconds=1
                )
            )
        )

    def kill(self):
        self._stop_tasks.append(
            asyncio.create_task(
                asyncio.to_thread(
                    self.runtime.stop, self.container_id, self.workspace_id, grace_seconds=0
                )
            )
        )

    async def aclose(self):
        self._pump.cancel()
        self.socket.close()
        # A stopped consumer can leave a full queue; don't wait on a final EOF put.
        if self.stdout.queue.full():
            self.stdout.queue.get_nowait()
        with suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(self._pump, *self._stop_tasks, return_exceptions=True), 1
            )
        with suppress(Exception):
            self.connection.close()


class _Input:
    def __init__(self, process):
        self.process, self.buffer = process, bytearray()

    def write(self, data):
        if len(self.buffer) + len(data) > MAX_LINE_BYTES + 1:
            raise BrokenPipeError("Connector command buffer is full")
        self.buffer.extend(data)

    async def drain(self):
        await asyncio.get_running_loop().sock_sendall(self.process.socket, bytes(self.buffer))
        self.buffer.clear()

    def close(self):
        with suppress(OSError):
            self.process.socket.shutdown(socket.SHUT_WR)


class _Output:
    def __init__(self):
        self.queue = asyncio.Queue(16)
        self.buffer = bytearray()
        self.eof, self.error = False, None

    async def readline(self):
        while True:
            if (index := self.buffer.find(b"\n")) != -1:
                data = bytes(self.buffer[: index + 1])
                del self.buffer[: index + 1]
                return data
            if len(self.buffer) > MAX_LINE_BYTES + 1:
                raise ValueError("Connector protocol record exceeds size limit")
            if self.eof:
                if self.error:
                    raise self.error
                data, self.buffer = bytes(self.buffer), bytearray()
                return data
            data = await self.queue.get()
            if data is None:
                self.eof = True
            else:
                self.buffer.extend(data)
