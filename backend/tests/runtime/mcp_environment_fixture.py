"""Local fake LLM plus MCP world service, executed only inside a task container."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls = 0

    def log_message(self, *_args):
        pass

    def send(self, status, value, content_type="application/json"):
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send(405, b"")

    def do_DELETE(self):
        self.send(200, b"")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/mcp":
            if "id" not in body:
                self.send(202, b"")
                return
            method = body["method"]
            if method == "initialize":
                result = {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fixture", "version": "1"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "write_result",
                            "description": "Write the world result",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                                "additionalProperties": False,
                            },
                        }
                    ]
                }
            elif method == "tools/call":
                assert body["params"]["name"] == "write_result"
                Path("/workspace/result.txt").write_text(body["params"]["arguments"]["text"])
                result = {
                    "content": [{"type": "text", "text": "World file created"}],
                    "isError": False,
                }
            elif method == "ping":
                result = {}
            else:
                self.send(
                    400,
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32601, "message": "Unknown fixture method"},
                    },
                )
                return
            self.send(200, {"jsonrpc": "2.0", "id": body["id"], "result": result})
            return
        assert self.path.endswith("/chat/completions")
        Handler.calls += 1
        index = Handler.calls
        tools = [item["function"]["name"] for item in body.get("tools", [])]
        selected = next((name for name in tools if name.endswith("write_result")), None)
        writing = selected is not None and not Path("/workspace/result.txt").exists()
        if writing:
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "world-write",
                        "type": "function",
                        "function": {
                            "name": selected,
                            "arguments": json.dumps(
                                {"text": "Written inside the MCP environment\n"}
                            ),
                        },
                    }
                ],
            }
        else:
            delta = {"role": "assistant", "content": "Completed"}
        common = {
            "id": f"fixture-{index}",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fake-model",
        }
        chunks = [
            {**common, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {
                **common,
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "tool_calls" if writing else "stop"}
                ],
            },
            {
                **common,
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        ]
        content = (
            b"".join(("data: " + json.dumps(value) + "\n\n").encode() for value in chunks)
            + b"data: [DONE]\n\n"
        )
        self.send(200, content, "text/event-stream")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    address = f"http://127.0.0.1:{server.server_port}"
    print(json.dumps({"model": address + "/v1", "mcp": address + "/mcp"}), flush=True)
    server.serve_forever()
