"""Exercise public discovery and effect refusal through the official MCP SDK."""

import asyncio
import json
import os
import sys

import pytest
from test_tool_catalog import document


def test_official_stdio_client_discovers_exact_schema_and_cannot_execute(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    path = tmp_path / "public-tools.json"
    value = document()
    path.write_text(json.dumps(value))

    async def scenario():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "evalhive_benchmark.tool_catalog", str(path)],
            env={"PYTHONPATH": os.pathsep.join(sys.path)},
        )
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert len(tools) == 1 and tools[0].name == "lookup"
                assert tools[0].inputSchema == value["tools"][0]["inputSchema"]
                result = await session.call_tool("lookup", {"id": "fixture"})
                assert result.isError
                assert "external benchmark driver" in result.content[0].text

    asyncio.run(scenario())
