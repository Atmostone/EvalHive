"""Exercise the real pinned browser MCP client against a local fixture page."""

import asyncio
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class Page(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        data = b"<html><title>Native browser fixture</title><h1>Confirmed native MCP browser</h1></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


async def main():
    spec = importlib.util.spec_from_file_location("task_client", "/opt/environment/task_client.py")
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    argv, private = client.launch_config(
        {"service": "playwright", "config": {"browser": "chromium", "headless": True}}
    )
    params = StdioServerParameters(
        command=argv[0],
        args=argv[1:],
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/tmp",
            **private,
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with asyncio.timeout(60), stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
                assert {"browser_navigate", "browser_evaluate"} <= {
                    tool.name for tool in tools.tools
                }
                opened = await session.call_tool(
                    "browser_navigate", {"url": "http://127.0.0.1:" + str(server.server_port)}
                )
                assert not opened.isError, opened
                evaluated = await session.call_tool(
                    "browser_evaluate", {"function": "() => document.querySelector('h1').innerText"}
                )
                assert not evaluated.isError, evaluated
                assert "Confirmed native MCP browser" in json.dumps(evaluated.model_dump())
                closed = await session.call_tool("browser_close", {})
                assert not closed.isError
                print(
                    json.dumps(
                        {
                            "server": initialized.serverInfo.model_dump(),
                            "tool_count": len(tools.tools),
                            "navigation": "confirmed",
                            "dom_result": "confirmed",
                            "close": "confirmed",
                        }
                    ),
                    flush=True,
                )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


asyncio.run(main())
