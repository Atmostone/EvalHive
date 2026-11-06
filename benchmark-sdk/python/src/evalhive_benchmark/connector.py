"""Connector dispatch with a separate reverse channel to the resource broker."""

from __future__ import annotations

import asyncio
import sys

from .peer import Context, Peer
from .protocol import MAX_LINE_BYTES, ProtocolError, validate, validate_environment, validate_spec

RESULT_TYPES = {
    "describe": "manifest",
    "list_cases": "case_page",
    "resolve_case": "case_descriptor",
    "validate": "validation_result",
    "plan": "environment_plan",
    "prepare": "prepared",
    "evaluate": "evaluation_result",
    "aggregate": "aggregate_result",
}


class Connector:
    manifest: dict

    async def handle(self, method: str, params: dict, context: Context):
        raise ProtocolError("UNSUPPORTED_CAPABILITY", "Connector does not implement this operation")

    async def close(self, context: Context):
        """Release process-local clients; durable cleanup is coordinated by the host."""


async def serve(connector: Connector, lines, write, **limits):
    validate("manifest", connector.manifest)
    context = None
    purpose = None

    async def handle(request):
        nonlocal purpose
        method, params = request["method"], request["params"]
        if method == "describe":
            return connector.manifest
        if method == "shutdown":
            context.cancelled.set()
            return {"stopped": True}
        if "spec" in params:
            validate_spec(connector.manifest, params["spec"])
            selected = params["spec"].get("purpose", "execution")
            if purpose is not None and selected != purpose:
                raise ProtocolError(
                    "PURPOSE_CONFLICT",
                    "A worker cannot change its execution purpose",
                    "configuration",
                )
            purpose = selected
        if "plan" in params:
            validate_environment(params["spec"], params["plan"])
        if method == "drive" and purpose == "evaluation":
            raise ProtocolError(
                "EVALUATION_ONLY", "An evaluation worker cannot drive an agent", "unsupported"
            )
        if method in {"prepare", "drive", "capture", "evaluate", "cleanup", "cancel"}:
            context.bind(params["spec"]["scope"] if "spec" in params else params["scope"])
        if method in {"prepare", "drive"} and context.cancelled.is_set():
            raise ProtocolError("CANCELLED", "Trial was cancelled")
        if method == "cancel":
            context.cancelled.set()
        if method in {"prepare", "drive", "capture", "evaluate", "aggregate", "cleanup"}:
            if connector.manifest["capabilities"].get(method) not in {"native", "emulated"}:
                raise ProtocolError("UNSUPPORTED_CAPABILITY", "Connector capability is unavailable")
        result = await connector.handle(method, params, context)
        if kind := RESULT_TYPES.get(method):
            validate(kind, result)
        if method in {"plan", "prepare"}:
            validate_environment(params["spec"], result, kind=RESULT_TYPES[method])
        return result

    peer = Peer(write, handler=handle, **limits)
    context = Context(peer)
    try:
        await peer.read(lines)
    finally:
        context.cancelled.set()
        await connector.close(context)


async def serve_stdio(connector: Connector):
    reader = asyncio.StreamReader(limit=MAX_LINE_BYTES + 2)
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)

    async def lines():
        while line := await reader.readline():
            yield line

    async def write(frame):
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()

    await serve(connector, lines(), write)
