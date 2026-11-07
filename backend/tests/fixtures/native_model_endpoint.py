"""A local OpenAI protocol fixture; no external model or provider is contacted."""

import json
import shlex
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests.runtime.agent_worker_fixture import TOOL_SCRIPT


class NativeModelEndpoint:
    def __init__(self, key):
        self.requests, self.errors = [], []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    assert self.path == "/v1/chat/completions"
                    assert self.headers.get("Authorization") == "Bearer " + key
                    length = int(self.headers["Content-Length"])
                    assert 0 < length <= 2 * 1024**2
                    body = json.loads(self.rfile.read(length))
                    first = not endpoint.requests
                    endpoint.requests.append(body)
                    if first:
                        assert any(tool["function"]["name"] == "bash" for tool in body["tools"])
                    command = "python -c " + shlex.quote(TOOL_SCRIPT)
                    delta = {"role": "assistant", "tool_calls": [{
                        "index": 0, "id": "native-shell", "type": "function", "function": {
                            "name": "bash", "arguments": json.dumps({
                                "command": command, "description": "Verify native task dependencies",
                            }),
                        },
                    }]} if first else {"content": "Completed"}
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for value in [
                        {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if first else "stop"}]},
                        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
                    ]:
                        data = {"id": "fixture-" + str(len(endpoint.requests)), "object": "chat.completion.chunk",
                                "created": 1, "model": "fake-model", **value}
                        self.wfile.write(("data: " + json.dumps(data) + "\n\n").encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except Exception as error:
                    endpoint.errors.append(type(error).__name__)
                    if not self.wfile.closed:
                        self.close_connection = True

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = "http://127.0.0.1:" + str(self.server.server_port) + "/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
