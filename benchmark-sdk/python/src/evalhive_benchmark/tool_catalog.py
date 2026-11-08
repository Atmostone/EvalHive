"""Public MCP discovery for tools whose effects belong to an external driver."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

MAX_BYTES = 1024**2


def load_catalog(path):
    path = Path(path).absolute()
    if path.resolve(strict=True) != path:
        raise ValueError("Public tool catalog cannot traverse links")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("Public tool catalog exceeds its regular-file budget")
        data = stream.read(MAX_BYTES + 1)
    if len(data) != info.st_size:
        raise ValueError("Public tool catalog changed while reading")
    document = json.loads(data)
    if (
        not isinstance(document, dict)
        or set(document) != {"format_version", "tools"}
        or document["format_version"] != "1.0"
        or not isinstance(document["tools"], list)
        or not 1 <= len(document["tools"]) <= 256
    ):
        raise ValueError("Public tool catalog has an unsupported format")
    names = set()
    for tool in document["tools"]:
        if (
            not isinstance(tool, dict)
            or set(tool) != {"name", "description", "inputSchema"}
            or not isinstance(tool["name"], str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", tool["name"])
            or tool["name"] in names
            or not isinstance(tool["description"], str)
            or not isinstance(tool["inputSchema"], dict)
            or tool["inputSchema"].get("type") != "object"
        ):
            raise ValueError("Public tool catalog contains an invalid or duplicate tool")
        Draft202012Validator.check_schema(tool["inputSchema"])
        names.add(tool["name"])
    return document


async def serve_catalog(document):
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool

    server = Server("evalhive-public-tool-catalog")

    @server.list_tools()
    async def list_tools():
        return [Tool(**tool) for tool in document["tools"]]

    @server.call_tool()
    async def call_tool(name, arguments):
        # This process contains neither environment state nor an effect handler.
        # The harness must hand requests to its external-step driver instead.
        raise ValueError("Tool execution requires the external benchmark driver")

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python -m evalhive_benchmark.tool_catalog PATH")
    asyncio.run(serve_catalog(load_catalog(sys.argv[1])))


if __name__ == "__main__":
    main()
