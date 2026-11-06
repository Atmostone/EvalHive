"""Process client; no backend, Docker, or native benchmark modules are imported."""

from __future__ import annotations

import asyncio
import math
import uuid
from contextlib import suppress

from .connector import RESULT_TYPES
from .peer import Peer
from .protocol import MAX_LINE_BYTES, ProtocolError, command, validate


async def _bounded(awaitable, timeout):
    """Detach from an unresponsive transport without waiting for its cancellation."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if done:
            return task.result()
        raise TimeoutError
    finally:
        if not task.done():
            task.cancel()
        # Resource cleanup may be fenced or finish after this transport detaches.
        # Its authoritative owner retains responsibility for the native resource.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())


class ConnectorClient:
    def __init__(self, process, *, broker=None, broker_replay=None, event_buffer=128, **limits):
        if type(event_buffer) is not int or event_buffer < 1:
            raise ValueError("Event buffer must be a positive integer")
        self.process = process
        self.queue = asyncio.Queue(event_buffer)
        self.error = None
        self.events_received = 0

        async def write(frame):
            self.process.stdin.write(frame)
            await self.process.stdin.drain()

        async def on_event(event):
            if self.queue.full():
                raise ProtocolError("EVENT_BUFFER_FULL", "Event consumer fell behind")
            self.queue.put_nowait(event)
            self.events_received += 1

        self.peer = Peer(
            write,
            handler=broker,
            replay_handler=broker_replay,
            request_type="broker_request",
            event_handler=on_event,
            **limits,
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

    async def request_receipt(self, method, params=None, *, request_id=None, timeout=30):
        """Return the wire success/error receipt for durable host storage.

        Transport failures still raise. Save the receipt before validating its
        connector-specific result, completing a delivery barrier, or ACKing it.
        """
        return await self.peer.request(
            command(request_id or uuid.uuid4().hex, method, params), timeout=timeout, receipt=True
        )

    async def resource(
        self,
        scope,
        provider,
        operation,
        payload,
        *,
        handle=None,
        request_id=None,
        timeout=30,
        durable_host=False,
    ):
        """Send an owned resource operation to an environment-provider worker."""
        from .peer import Context

        return await Context(self.peer).broker(
            scope,
            provider,
            operation,
            payload,
            handle=handle,
            request_id=request_id,
            timeout=timeout,
            durable_host=durable_host,
        )

    async def events(self):
        while not self.reading.done() or not self.queue.empty():
            try:
                yield await asyncio.wait_for(self.queue.get(), timeout=0.1)
            except TimeoutError:
                continue
        if self.error:
            raise self.error

    async def close(self, grace=3, *, shutdown=True):
        if (
            isinstance(grace, bool)
            or not isinstance(grace, (int, float))
            or not math.isfinite(grace)
            or not 0 < grace <= 86400
        ):
            raise ValueError("Close grace must be between 0 and 86400 seconds")
        incomplete = False
        try:
            if shutdown and not self.peer.closed:
                with suppress(ProtocolError, ConnectionError, TimeoutError):
                    await _bounded(self.request("shutdown", timeout=grace), grace)
            self.process.stdin.close()
            for signal in (None, self.process.terminate, self.process.kill):
                if signal is not None:
                    with suppress(ProcessLookupError):
                        signal()
                try:
                    await _bounded(self.process.wait(), grace)
                    break
                except TimeoutError:
                    continue
            else:
                incomplete = True
            try:
                cancellations = asyncio.current_task().cancelling()
                await _bounded(self.reading, grace)
            except TimeoutError:
                incomplete = True
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling() > cancellations:
                    raise
                # A locally detached reader is not cancellation of close's
                # caller. Report incomplete delivery using the protocol error.
                incomplete = True
        finally:
            if not self.reading.done():
                self.reading.cancel()
                self.reading.add_done_callback(
                    lambda done: None if done.cancelled() else done.exception()
                )
            if detach := getattr(self.process, "aclose", None):
                try:
                    await _bounded(detach(), grace)
                except TimeoutError:
                    incomplete = True
        if incomplete:
            self.error = ProtocolError(
                "TRANSPORT_CLOSE_INCOMPLETE",
                "Worker transport detached before confirmed process and stream termination",
                "recording",
            )
            raise self.error
