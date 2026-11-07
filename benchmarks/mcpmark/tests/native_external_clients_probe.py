"""Probe real external MCP server catalogs without network or real credentials."""

import asyncio
import importlib.util
import json

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    spec = importlib.util.spec_from_file_location("task_client", "/opt/environment/task_client.py")
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    for service, field in (("github", "github_token"), ("notion", "notion_key")):
        argv, private = client.launch_config(
            {"service": service, "config": {field: "fixture-only-no-external-account"}}
        )
        params = StdioServerParameters(
            command=argv[0],
            args=argv[1:],
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", **private},
        )
        async with asyncio.timeout(45), stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                assert listed.tools and all(tool.name and tool.inputSchema for tool in listed.tools)
                print(
                    json.dumps(
                        {
                            "service": service,
                            "server": initialized.serverInfo.model_dump(),
                            "tool_count": len(listed.tools),
                        }
                    ),
                    flush=True,
                )


asyncio.run(main())
