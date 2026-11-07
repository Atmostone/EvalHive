"""Use the official MCP server with public task APIs and no private dataset."""

import asyncio
import json
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    configuration = json.loads(Path("/evalhive-connection/connection.json").read_text())
    observed = set()
    for group in configuration["groups"]:
        params = StdioServerParameters(
            command="/opt/venv/bin/python",
            args=["/opt/environment/appworld_mcp.py", group["id"]],
            env={"PATH": "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"},
        )
        async with asyncio.timeout(90), stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                assert len(names) == group["tool_count"] and not observed & names
                assert not any(name.startswith(("admin__", "api_docs__")) for name in names)
                observed.update(names)
                if "supervisor__show_active_task" in names:
                    shown = await session.call_tool("supervisor__show_active_task", {})
                    assert not shown.is_error
                    completed = await session.call_tool(
                        "supervisor__complete_task",
                        {"status": "success", "answer": "Engineering fixture"},
                    )
                    assert not completed.is_error
                    assert "Marked the active task complete" in json.dumps(completed.model_dump())
    assert len(observed) == 469
    print(json.dumps({"official_mcp": True, "tools": len(observed), "mutation": True}), flush=True)


asyncio.run(main())
