"""MCP server exposing `web_search` (Tavily) and `web_fetch` (plain HTTP GET).

stdio MCP, mirroring time_server.py. The Tavily API key is read from the
TAVILY_API_KEY environment variable (set per-template via the MCP `env` field).
Used by the capability-isolation `fresh_data` benchmark cases.
"""

import os

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

server: Server = Server("web")

TAVILY_URL = "https://api.tavily.com/search"
_FETCH_CAP = 6000


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="web_search",
            description="Search the web (Tavily) and return the top results. Use for "
                        "current/post-cutoff facts the model cannot know on its own.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {"type": "integer", "description": "1-10, default 5."},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="web_fetch",
            description="Fetch the raw text content of a URL over HTTP(S).",
            inputSchema={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "URL to fetch."}},
                "required": ["url"],
            },
        ),
    ]


async def _web_search(query: str, max_results: int) -> str:
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return "ERROR: TAVILY_API_KEY is not set for this MCP server."
    payload = {
        "api_key": key,
        "query": query,
        "max_results": max(1, min(10, int(max_results or 5))),
        "include_answer": True,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(TAVILY_URL, json=payload)
        r.raise_for_status()
        data = r.json()
    lines: list[str] = []
    if data.get("answer"):
        lines.append(f"Answer: {data['answer']}")
    for i, res in enumerate(data.get("results") or [], 1):
        lines.append(
            f"{i}. {res.get('title', '')}\n   {res.get('url', '')}\n   "
            f"{(res.get('content') or '')[:500]}"
        )
    return "\n".join(lines) if lines else "(no results)"


async def _web_fetch(url: str) -> str:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        r = await client.get(url, headers={"User-Agent": "SpawnHive-agent/1.0"})
        r.raise_for_status()
        text = r.text
    return text[:_FETCH_CAP] + ("\n…[truncated]" if len(text) > _FETCH_CAP else "")


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "web_search":
            out = await _web_search(arguments.get("query", ""), arguments.get("max_results", 5))
        elif name == "web_fetch":
            out = await _web_fetch(arguments.get("url", ""))
        else:
            out = f"unknown tool: {name}"
    except Exception as e:  # noqa: BLE001 — surface errors to the agent, never crash the server
        out = f"ERROR: {name} failed: {e}"
    return [TextContent(type="text", text=out)]


async def _main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
