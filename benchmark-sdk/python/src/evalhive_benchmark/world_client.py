"""Drive private native service operations through an owned environment handle."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time

from .protocol import ProtocolError


class WorldClient:
    def __init__(self, scope, handle, *, deadline, provider="evalhive.rootless", prefix="native"):
        self.scope, self.handle, self.deadline = scope, handle, deadline
        self.provider, self.prefix, self.sequence = provider, prefix, 0

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise ProtocolError("NATIVE_PHASE_TIMEOUT", "Native operation expired", "execution")
        return value

    async def control(self, context, operation, payload):
        self.sequence += 1
        identity = self.prefix + ".control." + str(self.sequence)
        request = {"id": identity, "scope": self.scope, "operation": operation, "payload": payload}
        encoded = base64.b64encode(json.dumps(request, separators=(",", ":")).encode()).decode()
        result = await context.broker(
            self.scope,
            self.provider,
            "exec",
            {
                "argv": [
                    "/opt/venv/bin/python",
                    "/opt/benchmark-sdk/evalhive_benchmark/world_wire.py",
                    encoded,
                ],
                "cwd": "/tmp",
                "timeout_seconds": self.remaining(),
                "deadline_policy": "inherit",
                "process_policy": "stop_after_command",
            },
            handle=self.handle,
            request_id=identity,
            timeout=self.remaining() + 5,
            durable_host=True,
        )
        if result["exit_code"] != 0:
            raise ProtocolError(
                "NATIVE_CONTROL_FAILED", "Private native operation failed", "execution"
            )
        try:
            return json.loads(result["stdout"])
        except (TypeError, ValueError, KeyError):
            raise ProtocolError(
                "NATIVE_CONTROL_FAILED", "Private native receipt is invalid", "recording"
            ) from None

    async def call(self, context, method, params=None, *, effect=None):
        started = await self.control(context, "begin", {"method": method, "params": params or {}})
        while True:
            result = await self.control(context, "poll", {"operation_id": started["operation_id"]})
            for pending in result["effects"]:
                if effect is None:
                    raise ProtocolError(
                        "NATIVE_EFFECT_UNAVAILABLE",
                        "Native transport is unavailable",
                        "unsupported",
                    )
                receipt = await effect(pending)
                await self.control(
                    context, "resume", {"effect_id": pending["id"], "result": receipt}
                )
            if result["state"] == "completed":
                return result["result"]
            if result["state"] == "failed":
                name = result.get("error", {}).get("exception", "UnknownError")
                if not isinstance(name, str) or not re.fullmatch(
                    r"[A-Za-z_][A-Za-z_0-9]{0,79}", name
                ):
                    name = "UnknownError"
                raise ProtocolError(
                    "NATIVE_SERVICE_FAILED", f"Native service {method} failed ({name})", "execution"
                )
            await asyncio.sleep(min(0.2, self.remaining()))
