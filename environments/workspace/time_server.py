"""Tiny MCP server exposing a single tool `now` returning the current UTC time."""

import asyncio
from datetime import UTC, datetime

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

server: Server = Server("time")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="now",
            description="Return the current UTC time as an ISO-8601 string.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "now":
        return [TextContent(type="text", text=datetime.now(UTC).isoformat())]
    return [TextContent(type="text", text=f"unknown tool: {name}")]


async def _main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
