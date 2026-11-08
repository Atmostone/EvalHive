"""Environment providers use scoped resource RPC without backend imports."""

from __future__ import annotations

from .peer import Context, Peer
from .protocol import ProtocolError, validate


class EnvironmentProvider:
    manifest: dict

    async def handle(self, request: dict, context: Context):
        """The validated request includes scope and stable ID for native idempotency."""
        raise ProtocolError("UNSUPPORTED_CAPABILITY", "Provider operation is unavailable", "unsupported")

    async def close(self, context: Context):
        """Close local clients; the coordinator owns durable resource release."""


async def serve_provider(provider: EnvironmentProvider, lines, write, **limits):
    validate("manifest", provider.manifest)
    if provider.manifest["kind"] != "environment_provider":
        raise ProtocolError("INVALID_MANIFEST", "Expected an environment-provider manifest", "configuration")
    context = None

    async def handle(request):
        if request["type"] == "command":
            method, params = request["method"], request["params"]
            if method == "describe":
                return provider.manifest
            if method == "shutdown":
                context.cancelled.set()
                return {"stopped": True}
            if method == "cancel":
                context.bind(params["scope"])
                context.cancelled.set()
                return {"accepted": True}
            raise ProtocolError("UNSUPPORTED_CAPABILITY", "Provider command is unavailable", "unsupported")
        if request["provider"] != provider.manifest["id"]:
            raise ProtocolError("PROVIDER_MISMATCH", "Resource operation targets another provider", "configuration")
        context.bind(request["scope"])
        operation = request["operation"]
        if provider.manifest["capabilities"].get(operation) not in {"native", "emulated"}:
            raise ProtocolError("UNSUPPORTED_CAPABILITY", "Provider operation is unavailable", "unsupported")
        if operation in {"allocate", "exec"} and context.cancelled.is_set():
            raise ProtocolError("CANCELLED", "Provider generation was cancelled", "execution")
        return await provider.handle(request, context)

    peer = Peer(write, handler=handle, request_type=("command", "broker_request"), **limits)
    context = Context(peer)
    try:
        await peer.read(lines)
    finally:
        context.cancelled.set()
        await provider.close(context)


async def serve_provider_stdio(provider: EnvironmentProvider):
    import asyncio
    import sys

    from .protocol import MAX_LINE_BYTES

    reader = asyncio.StreamReader(limit=MAX_LINE_BYTES + 2)
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)

    async def lines():
        while line := await reader.readline():
            yield line

    async def write(frame):
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()

    await serve_provider(provider, lines(), write)
