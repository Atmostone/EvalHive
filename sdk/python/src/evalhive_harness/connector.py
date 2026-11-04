"""Connector-side dispatch. Process supervision and durable delivery belong to the runner."""

from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import AsyncIterable, Awaitable, Callable
from typing import Any

from .protocol import (
    MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    decode_line,
    encode_line,
    error_response,
    response,
    validate,
    validate_run,
)


class RunContext:
    def __init__(self, spec: dict, send: Callable[[dict], Awaitable[None]]):
        self.spec = spec
        self.cancelled = asyncio.Event()
        self._send = send
        self._seq = 0
        self._lock = asyncio.Lock()
        self._finished = False

    async def emit(
        self,
        event: str,
        data: dict,
        *,
        source: str = "main",
        session_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> None:
        if event in ("run.started", "run.finished"):
            raise ProtocolError("RESERVED_EVENT", "Run lifecycle is owned by the SDK")
        await self._emit(
            event, data, source=source, session_id=session_id, parent_session_id=parent_session_id
        )

    async def _emit(self, event: str, data: dict, **extra) -> None:
        async with self._lock:
            if self._finished:
                raise ProtocolError("ATTEMPT_FINISHED", "Attempt has finished")
            value = {
                "protocol_version": PROTOCOL_VERSION,
                "type": "event",
                "event_id": uuid.uuid4().hex,
                "attempt_id": self.spec["attempt_id"],
                "seq": self._seq,
                "source": extra.pop("source", "main"),
                "event": event,
                "data": data,
                **{k: v for k, v in extra.items() if v is not None},
            }
            validate("event", value)
            await self._send(value)
            self._seq += 1
            if event == "run.finished":
                self._finished = True


class Connector:
    manifest: dict

    async def validate(self, spec: dict) -> dict:
        return {"accepted": True, "effective_config": spec["config"]}

    async def run(self, context: RunContext) -> dict:
        raise NotImplementedError

    async def control(self, context: RunContext, name: str, payload: dict) -> Any:
        raise ProtocolError("UNSUPPORTED_CAPABILITY", f"Unavailable command: {name}")


async def serve(
    connector: Connector, lines: AsyncIterable[bytes], write: Callable[[bytes], Awaitable[None]]
) -> None:
    validate("manifest", connector.manifest)
    output_lock = asyncio.Lock()
    run_task: asyncio.Task | None = None
    context: RunContext | None = None
    started = False

    async def send(value):
        data = encode_line(value)
        async with output_lock:
            await write(data)

    async def execute(ctx):
        await ctx._emit("run.started", {})
        try:
            result = await connector.run(ctx)
            validate("result", result)
            if ctx.cancelled.is_set():
                result = {"status": "cancelled", "summary": "Run cancelled"}
        except asyncio.CancelledError:
            result = {"status": "cancelled", "summary": "Run cancelled"}
        except ProtocolError as exc:
            result = {"status": "failed", "summary": "Connector failed", "error": exc.as_dict()}
        except Exception:
            result = {
                "status": "failed",
                "summary": "Connector failed",
                "error": {"code": "CONNECTOR_ERROR", "message": "Connector execution failed"},
            }
        await ctx._emit("run.finished", result)

    async def stop():
        if run_task is not None and not run_task.done():
            context.cancelled.set()
            # Let execute() enter its cancellation handler even for immediate shutdown.
            await asyncio.sleep(0)
            if run_task.done():
                await run_task
                return
            run_task.cancel()
            await run_task

    try:
        async for line in lines:
            request_id = None
            try:
                value = decode_line(line)
                if value["type"] != "command":
                    raise ProtocolError("INVALID_MESSAGE", "Expected a command")
                request_id = value["id"]
                method, params = value["method"], value["params"]
                if method == "describe":
                    result = connector.manifest
                elif method in ("validate", "start"):
                    spec = validate_run(connector.manifest, params["run"])
                    if method == "start" and started:
                        raise ProtocolError("ALREADY_STARTED", "One process executes one attempt")
                    result = await connector.validate(spec)
                    if method == "start":
                        context = RunContext(spec, send)
                        started = True
                        await send(response(request_id, {"accepted": True}))
                        run_task = asyncio.create_task(execute(context))
                        continue
                elif method == "control":
                    if (
                        context is None
                        or run_task.done()
                        or params["attempt_id"] != context.spec["attempt_id"]
                    ):
                        raise ProtocolError("ATTEMPT_NOT_ACTIVE", "Attempt is not active")
                    name = params["command"]
                    if connector.manifest["capabilities"].get(name) not in ("native", "emulated"):
                        raise ProtocolError(
                            "UNSUPPORTED_CAPABILITY", f"Unavailable command: {name}"
                        )
                    payload = params.get("payload", {})
                    command_schema = connector.manifest.get("commands", {}).get(name)
                    if command_schema:
                        validate_run(
                            {**connector.manifest, "config_schema": command_schema["input_schema"]},
                            {**context.spec, "config": payload, "required_capabilities": []},
                        )
                    result = await connector.control(context, name, payload)
                else:  # shutdown
                    await stop()
                    await send(response(request_id, {"stopped": True}))
                    break
                await send(response(request_id, result))
            except ProtocolError as exc:
                await send(error_response(request_id, exc))
            except Exception:
                await send(
                    error_response(request_id, ProtocolError("CONNECTOR_ERROR", "Command failed"))
                )
    except ProtocolError as exc:
        await send(error_response(None, exc))
    finally:
        await stop()


async def serve_stdio(connector: Connector) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_LINE_BYTES + 2)
    transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    out_transport, out_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(out_transport, out_protocol, None, loop)

    async def lines():
        while True:
            try:
                line = await reader.readline()
            except ValueError:
                # Reject and stop: don't accidentally interpret the tail as another command.
                raise ProtocolError("FRAME_TOO_LARGE", "Protocol record exceeds size limit") from None
            if not line:
                return
            yield line

    async def write(data):
        writer.write(data)
        await writer.drain()

    try:
        await serve(connector, lines(), write)
    finally:
        transport.close()
        writer.close()
