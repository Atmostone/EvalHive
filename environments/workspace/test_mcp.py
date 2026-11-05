"""Exercise the installed environment servers without external network access."""

import asyncio
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Local environment fixture")

    def log_message(self, *_):
        pass


async def check(server, expected, call, arguments):
    async with stdio_client(StdioServerParameters(command="python", args=[server])) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            assert {tool.name for tool in (await session.list_tools()).tools} == expected
            result = await session.call_tool(call, arguments)
            assert not result.isError
            return result.content[0].text


async def main():
    stamp = await check("/app/time_server.py", {"now"}, "now", {})
    assert datetime.fromisoformat(stamp).utcoffset().total_seconds() == 0
    http = HTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        text = await check("/app/web_server.py", {"web_search", "web_fetch"}, "web_fetch",
                           {"url": f"http://127.0.0.1:{http.server_port}/fixture"})
        assert text == "Local environment fixture"
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=3)
    print("Workspace environment MCP checks passed (zero external requests)")


if __name__ == "__main__":
    asyncio.run(main())
