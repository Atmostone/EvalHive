"""Local responses exercise the original DVD Rental indexing task through MCP."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MCPMarkPostgresEndpoint:
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
                    assert body["model"] == "fixture-mcpmark-pg"
                    step = len(endpoint.requests)
                    endpoint.requests.append(body)
                    tools = [tool["function"]["name"] for tool in body["tools"]]
                    assert not {"bash", "read", "write", "edit"} & set(tools)
                    message = {"role": "assistant", "content": "Completed"}
                    if step < 2:
                        name = next(name for name in tools if name.endswith("execute_sql"))
                        sql = (
                            "SELECT current_database() AS database, COUNT(*) AS payments FROM public.payment"
                            if step == 0
                            else "CREATE INDEX evalhive_engineering_customer_id ON public.payment(customer_id)"
                        )
                        message = {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "native-sql-" + str(step),
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps({"sql": sql}),
                                    },
                                }
                            ],
                        }
                    common = {"id": "mcpmark-pg-" + str(step), "created": 1, "model": body["model"]}
                    finish = "tool_calls" if step < 2 else "stop"
                    usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
                    self.send_response(200)
                    if body.get("stream"):
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        delta = {**message}
                        if step < 2:
                            delta["tool_calls"] = [{"index": 0, **message["tool_calls"][0]}]
                        for value in [
                            {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                            {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                            {"choices": [], "usage": usage},
                        ]:
                            self.wfile.write(
                                (
                                    "data: "
                                    + json.dumps(
                                        {**common, "object": "chat.completion.chunk", **value}
                                    )
                                    + "\n\n"
                                ).encode()
                            )
                        self.wfile.write(b"data: [DONE]\n\n")
                    else:
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(
                            json.dumps(
                                {
                                    **common,
                                    "object": "chat.completion",
                                    "choices": [
                                        {"index": 0, "message": message, "finish_reason": finish}
                                    ],
                                    "usage": usage,
                                }
                            ).encode()
                        )
                    self.wfile.flush()
                except Exception as error:
                    endpoint.errors.append(type(error).__name__)
                    self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = "http://127.0.0.1:" + str(self.server.server_port) + "/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
