"""Host-side connector process client with bounded pending requests."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress

from .protocol import MAX_LINE_BYTES, ProtocolError, command, decode_line, encode_line, validate


class ConnectorClient:
    def __init__(self, process: asyncio.subprocess.Process, *, max_pending=64, event_buffer=128):
        if max_pending < 1 or event_buffer < 1:
            raise ValueError("Buffer limits must be positive")
        self.process = process
        self.max_pending = max_pending
        self._pending: dict[str, asyncio.Future] = {}
        self._events: asyncio.Queue = asyncio.Queue(event_buffer)
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._closing = False
        self._stream_error: ProtocolError | None = None
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def launch(
        cls, argv: list[str], *, cwd: str | None = None, env: dict | None = None, **kwargs
    ):
        if not argv or not all(isinstance(x, str) and x for x in argv):
            raise ValueError("Entrypoint must be a nonempty argv list")
        for name in ("max_pending", "event_buffer"):
            limit = kwargs.get(name, 64 if name == "max_pending" else 128)
            if type(limit) is not int or limit < 1:
                raise ValueError("Buffer limits must be positive integers")
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
            limit=MAX_LINE_BYTES + 2,
        )
        return cls(process, **kwargs)

    async def request(self, method: str, params: dict | None = None, *, timeout: float = 30):
        if self._closed:
            raise ProtocolError("TRANSPORT_CLOSED", "Connector transport is closed")
        if len(self._pending) >= self.max_pending:
            raise ProtocolError("PENDING_LIMIT", "Too many pending requests")
        request_id = uuid.uuid4().hex
        data = encode_line(command(request_id, method, params))
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            try:
                async with asyncio.timeout(timeout):
                    async with self._write_lock:
                        self.process.stdin.write(data)
                        await self.process.stdin.drain()
                    result = await future
            except TimeoutError:
                raise ProtocolError("TIMEOUT", "Connector request timed out") from None
            except (BrokenPipeError, ConnectionResetError):
                raise ProtocolError("TRANSPORT_CLOSED", "Connector transport is closed") from None
            if method == "describe":
                validate("manifest", result)
            return result
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Observe a read error even if writing failed first.

    async def _read(self):
        error = ProtocolError("TRANSPORT_CLOSED", "Connector disconnected")
        try:
            while line := await self.process.stdout.readline():
                value = decode_line(line)
                if value["type"] == "event":
                    # A consumer must drain events while a run is active.
                    if self._events.full():
                        raise ProtocolError("EVENT_BUFFER_FULL", "Event consumer fell behind")
                    self._events.put_nowait(value)
                elif value["type"] == "response":
                    if value["id"] is None:
                        raise ProtocolError(value["error"]["code"], value["error"]["message"])
                    pending = self._pending.get(value["id"])
                    if pending is None or pending.done():
                        continue  # reply to a timed-out request
                    if value["ok"]:
                        pending.set_result(value["result"])
                    else:
                        pending.set_exception(
                            ProtocolError(value["error"]["code"], value["error"]["message"])
                        )
                else:
                    raise ProtocolError("INVALID_MESSAGE", "Unexpected command from connector")
                # Let an active consumer run even when many complete records
                # arrived in one pipe read; the bounded queue is not a burst cap.
                await asyncio.sleep(0)
        except ProtocolError as exc:
            error = exc
        except (ValueError, OSError):
            error = ProtocolError("INVALID_FRAME", "Connector stream is invalid")
        finally:
            self._closed = True
            for pending in self._pending.values():
                if not pending.done():
                    pending.set_exception(error)
            self._stream_error = (
                None if self._closing and error.code == "TRANSPORT_CLOSED" else error
            )

    async def events(self) -> AsyncIterator[dict]:
        while not self._closed or not self._events.empty():
            try:
                event = await asyncio.wait_for(self._events.get(), 0.1)
            except TimeoutError:
                continue
            yield event
        if self._stream_error:
            raise self._stream_error

    async def close(self, *, grace: float = 3):
        self._closing = True
        if not self._closed:
            with suppress(ProtocolError, BrokenPipeError, ConnectionResetError):
                await self.request("shutdown", timeout=grace)
        if self.process.stdin:
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), grace)
        except TimeoutError:
            with suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), grace)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self.process.kill()
                try:
                    await asyncio.wait_for(self.process.wait(), grace)
                except TimeoutError:
                    raise ProtocolError(
                        "SHUTDOWN_FAILED", "Connector did not exit after forced stop"
                    ) from None
        self._reader.cancel()
        with suppress(asyncio.CancelledError):
            await self._reader
