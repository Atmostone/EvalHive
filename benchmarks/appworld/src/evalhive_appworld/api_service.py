"""Own one native API process with a separate, restricted task-facing proxy."""

from __future__ import annotations

import asyncio
import json
import os
import threading

from .api_gate import Gate, Surface, proxy

PRIVATE_PORT, PUBLIC_PORT = 9000, 8080
CONTROL_PREFIX = "/.__evalhive/"


class PrivateControls:
    def __init__(self, app, gate):
        self.app, self.gate = gate.wrap_native(app), gate

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(CONTROL_PREFIX):
            return await self.app(scope, receive, send)
        status, result = 403, {"error": "Private control is unavailable"}
        client = scope.get("client")
        if client and client[0] == "127.0.0.1":
            operation = scope["path"][len(CONTROL_PREFIX) :]
            if operation == "status" and scope["method"] == "GET":
                status, result = 200, {"ready": True, "accepting": self.gate.accepting}
            elif operation == "open" and scope["method"] == "POST":
                self.gate.open()
                status, result = 200, {"accepting": True}
            elif operation == "seal" and scope["method"] == "POST":
                try:
                    # Native requests must be allowed to finish on the ASGI event loop.
                    await asyncio.to_thread(self.gate.seal, timeout=20)
                    status, result = 200, {"sealed": True}
                except TimeoutError:
                    status, result = 503, {"error": "Native API effects did not quiesce"}
        data = json.dumps(result, separators=(",", ":")).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(data)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": data})


def main():
    from .native import load

    load(os.environ["APPWORLD_ROOT"])
    import uvicorn
    from appworld.collections.api_docs import ApiDocCollection
    from appworld.serve._apis import app
    from appworld.serve._mcp import DEFAULT_APP_NAMES

    gate = Gate()
    surface = Surface(ApiDocCollection.build(load_apps=DEFAULT_APP_NAMES))
    public = proxy(surface, gate, PRIVATE_PORT, address=("0.0.0.0", PUBLIC_PORT))
    thread = threading.Thread(target=public.serve_forever, daemon=True)
    thread.start()
    try:
        uvicorn.run(
            PrivateControls(app, gate),
            host="127.0.0.1",
            port=PRIVATE_PORT,
            loop="uvloop",
            access_log=False,
            log_level="warning",
            proxy_headers=False,
        )
    finally:
        public.shutdown()
        public.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
