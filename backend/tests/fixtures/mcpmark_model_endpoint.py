"""Offline responses exercise original MCPMark Filesystem effects through real MCP."""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MCPMarkModelEndpoint:
    def __init__(self, key, initial_files):
        self.requests, self.errors = [], []
        self.responses = 0
        endpoint = self

        def calls(body, step):
            names = [tool["function"]["name"] for tool in body["tools"]]
            assert not {"bash", "read", "write", "edit"} & set(names)

            def call(suffix, arguments, identity):
                name = next(name for name in names if name.endswith(suffix))
                return {
                    "id": identity,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }

            if step == 0:
                return [call("list_allowed_directories", {}, "allowed-directory")]
            if step >= 4:
                return None
            history = json.dumps(body["messages"])
            root = re.search(r"/opt/native/\.mcpmark_backups/backup_[A-Za-z0-9_]+", history).group()
            if step == 1:
                return [
                    call(
                        "read_multiple_files",
                        {"paths": [root + "/" + name for name in initial_files]},
                        "read-native-input",
                    )
                ]
            if step == 2:
                return [
                    call("create_directory", {"path": root + "/uppercase"}, "create-native-output")
                ]
            effects = [
                call(
                    "write_file",
                    {"path": root + "/uppercase/" + name, "content": content.upper()},
                    "write-" + name.replace(".", "-"),
                )
                for name, content in initial_files.items()
                if int(name[5:7]) <= 5
            ]
            answer = (
                "\n".join(
                    name + ":" + str(len(content.split()))
                    for name, content in initial_files.items()
                )
                + "\n"
            )
            effects.append(
                call(
                    "write_file",
                    {"path": root + "/uppercase/answer.txt", "content": answer},
                    "write-native-answer",
                )
            )
            return effects

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
                    assert body["model"] == "fixture-mcpmark"
                    step = endpoint.responses
                    endpoint.requests.append(body)
                    tool_calls = calls(body, step)
                    message = {"role": "assistant", "content": None if tool_calls else "Completed"}
                    if tool_calls:
                        message["tool_calls"] = tool_calls
                    common = {"id": "mcpmark-" + str(step), "created": 1, "model": body["model"]}
                    finish = "tool_calls" if tool_calls else "stop"
                    usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
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
                    endpoint.responses += 1
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
