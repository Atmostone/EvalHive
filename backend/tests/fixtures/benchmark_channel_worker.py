"""Standalone SDK worker for host-channel engineering checks; no backend imports."""

import asyncio
import os
import uuid

from evalhive_benchmark import Connector, ProtocolError, serve_stdio


class Worker(Connector):
    manifest = {
        "protocol_version": "1.0",
        "kind": "benchmark",
        "id": "fixture.channel",
        "version": "1.0",
        "display_name": "Channel fixture",
        "entrypoint": ["fixture"],
        "image": "fixture",
        "config_schema": {"type": "object"},
        "requirements": {},
        "capabilities": {name: "native" for name in ["drive", "capture", "cleanup"]},
    }

    def __init__(self):
        self.calls = 0

    async def handle(self, method, params, context):
        if method == "drive":
            self.calls += 1
            payload, scope = params["payload"], params["scope"]
            if payload.get("die"):
                os._exit(9)
            if payload.get("fail"):
                raise ProtocolError(
                    "FIXTURE_REJECTED", "Private worker fixture detail", "execution"
                )
            if payload.get("foreign"):
                scope = {**scope, "workspace_id": str(uuid.uuid4())}
            await context.emit(scope, "native", {"detail": "private-fixture-observation"})
            if payload.get("wait"):
                await context.cancelled.wait()
                return {"finished": False, "cancelled": True, "calls": self.calls}
            await context.emit(scope, "native", {"detail": "second-observation"}, source="other")
            return {"finished": True, "calls": self.calls}
        if method == "capture":
            await context.emit(params["scope"], "native", {"capture": True}, source="capture")
            return {"artifacts": []}
        if method == "cancel":
            return {"cancelled": True}
        if method == "cleanup":
            return {"cleaned": True}
        raise ProtocolError("UNSUPPORTED_CAPABILITY", "Fixture operation unavailable")


asyncio.run(serve_stdio(Worker()))
