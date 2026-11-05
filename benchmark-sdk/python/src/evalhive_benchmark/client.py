"""Process client; no backend, Docker, or native benchmark modules are imported."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress

from .connector import RESULT_TYPES
from .peer import Peer
from .protocol import MAX_LINE_BYTES, ProtocolError, command, validate


class ConnectorClient:
    def __init__(self, process, *, broker=None, event_buffer=128, **limits):
        if type(event_buffer) is not int or event_buffer < 1:
            raise ValueError("Event buffer must be a positive integer")
        self.process = process
        self.queue = asyncio.Queue(event_buffer)
        self.error = None

        async def write(frame):
            self.process.stdin.write(frame)
            await self.process.stdin.drain()

        async def on_event(event):
            if self.queue.full():
                raise ProtocolError("EVENT_BUFFER_FULL", "Event consumer fell behind")
            self.queue.put_nowait(event)

        self.peer = Peer(
            write, handler=broker, request_type="broker_request", event_handler=on_event, **limits
        )
        self.reading = asyncio.create_task(self._read())

    @classmethod
    async def launch(cls, argv, *, cwd=None, env=None, **options):
        if not argv or not all(
            isinstance(value, str) and value and "\0" not in value for value in argv
        ):
            raise ValueError("Entrypoint must be a nonempty argv list")
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
            limit=MAX_LINE_BYTES + 2,
        )
        try:
            return cls(process, **options)
        except Exception:
            process.kill()
            await process.wait()
            raise

    async def _read(self):
        async def lines():
            while line := await self.process.stdout.readline():
                yield line

        try:
            await self.peer.read(lines())
        except ProtocolError as error:
            self.error = error

    async def request(self, method, params=None, *, request_id=None, timeout=30):
        result = await self.peer.request(
            command(request_id or uuid.uuid4().hex, method, params), timeout=timeout
        )
        if kind := RESULT_TYPES.get(method):
            validate(kind, result)
        return result

    async def resource(self, scope, provider, operation, payload, *, handle=None, request_id=None, timeout=30):
        """Send an owned resource operation to an environment-provider worker."""
        from .peer import Context

        return await Context(self.peer).broker(scope, provider, operation, payload,
            handle=handle, request_id=request_id, timeout=timeout)

    async def events(self):
        while not self.reading.done() or not self.queue.empty():
            try:
                yield await asyncio.wait_for(self.queue.get(), timeout=0.1)
            except TimeoutError:
                continue
        if self.error:
            raise self.error

    async def close(self, grace=3):
        if not self.peer.closed:
            with suppress(ProtocolError, ConnectionError):
                await self.request("shutdown", timeout=grace)
        self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), grace)
        except TimeoutError:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), grace)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        await self.reading
