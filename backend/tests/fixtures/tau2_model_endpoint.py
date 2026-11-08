"""Local protocol responses for real Harness and native user simulator clients."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Tau2ModelEndpoint:
    def __init__(self, agent_key, user_key):
        self.requests, self.errors = {"agent": [], "user": []}, []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    assert self.path == "/v1/chat/completions"
                    length = int(self.headers["Content-Length"])
                    assert 0 < length <= 1024**2
                    body = json.loads(self.rfile.read(length))
                    role = "user" if body["model"] == "fixture-user" else "agent"
                    assert body["model"] == "fixture-" + role
                    assert self.headers.get("Authorization") == "Bearer " + (
                        user_key if role == "user" else agent_key
                    )
                    endpoint.requests[role].append(body)
                    first = len(endpoint.requests[role]) == 1
                    tool_calls = None
                    if role == "agent" and first:
                        names = [tool["function"]["name"] for tool in body["tools"]]
                        assert names and not ({"bash", "read", "write", "edit"} & set(names))
                        name = next(name for name in names if name.endswith("calculate"))
                        tool_calls = [
                            {
                                "id": identity,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps({"expression": expression}),
                                },
                            }
                            for identity, expression in (
                                ("arithmetic-a", "2+2"),
                                ("arithmetic-b", "3+3"),
                            )
                        ]
                    content = (
                        "Please calculate 2+2 and 3+3."
                        if role == "user" and first
                        else "###STOP###"
                        if role == "user"
                        else "Done"
                    )
                    message = {"role": "assistant", "content": None if tool_calls else content}
                    if tool_calls:
                        message["tool_calls"] = tool_calls
                    finish = "tool_calls" if tool_calls else "stop"
                    common = {
                        "id": "fixture-" + role + "-" + str(len(endpoint.requests[role])),
                        "created": 1,
                        "model": body["model"],
                    }
                    usage = {
                        "prompt_tokens": 7 if role == "user" else 10,
                        "completion_tokens": 4 if role == "user" else 2,
                        "total_tokens": 11 if role == "user" else 12,
                    }
                    self.send_response(200)
                    if body.get("stream"):
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        delta = {**message}
                        if tool_calls:
                            delta["tool_calls"] = [
                                {"index": index, **call} for index, call in enumerate(tool_calls)
                            ]
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
