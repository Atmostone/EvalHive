"""Example provider package; it implements no Docker/backend-specific branch."""

import asyncio
import json
from pathlib import Path

from evalhive_benchmark import EnvironmentProvider, ProtocolError, serve_provider_stdio


class ExampleProvider(EnvironmentProvider):
    manifest = json.loads(Path(__file__).with_name("provider-manifest.json").read_text())

    def __init__(self):
        self.allocated = 0
        self.handles = {}

    async def handle(self, request, context):
        operation, payload, scope, handle = request["operation"], request["payload"], request["scope"], request.get("handle")
        if operation == "allocate":
            self.allocated += 1
            identity = f"owned-{self.allocated}"
            self.handles[identity] = {"stopped": False}
            await context.emit(scope, "native", {"name": "resource.allocated", "payload": {"handle": identity}}, source="provider")
            return {"handle": identity}
        if handle not in self.handles:
            raise ProtocolError("FOREIGN_HANDLE", "Resource handle is unavailable", "configuration")
        if operation == "inspect":
            return {"allocated": self.allocated, **self.handles[handle]}
        if operation == "exec":
            if payload.get("fail"):
                raise RuntimeError("private-native-provider-diagnostic")
            if payload.get("wait"):
                await context.cancelled.wait()
                raise ProtocolError("CANCELLED", "Owned execution cancelled", "execution")
            return {"exit_code": 0, "stdout": "Owned fixture execution"}
        if operation == "stop":
            self.handles[handle]["stopped"] = True
            return {"stopped": True}
        if operation == "release":
            self.handles.pop(handle)
            return {"released": True}
        raise ProtocolError("UNSUPPORTED_CAPABILITY", "Provider operation is unavailable", "unsupported")


if __name__ == "__main__":
    asyncio.run(serve_provider_stdio(ExampleProvider()))
