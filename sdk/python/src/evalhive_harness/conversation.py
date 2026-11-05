"""Persistent input and external-tool boundaries; native connectors own generation."""

from __future__ import annotations

import asyncio
import hashlib
import json

from .protocol import ProtocolError, validate

COMMANDS = {
    "turn.submit": "conversation_input",
    "tool.respond": "conversation_tool_response",
    "session.close": "conversation_close",
}
EVENTS = {"turn.started", "turn.output", "turn.completed", "session.waiting_input", "tool.request"}


def signature(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class Conversation:
    def __init__(self, context):
        self.context = context
        self.config = context.spec["interaction"]
        self.mode = self.config["mode"]
        self.revision = 0
        self.state = "waiting_input"
        self.current = None
        self.close_reason = None
        self._inputs = asyncio.Queue(maxsize=1)
        self._receipts = {}
        self._turn_ids = set()
        self._batches = set()
        self._pending = None
        self._tool_count = 0
        self._lock = asyncio.Lock()

    async def ready(self):
        async with self._lock:
            if self.state == "waiting_input":
                await self.context._emit("session.waiting_input", {"revision": self.revision})

    async def _wait(self, awaitable):
        work = asyncio.ensure_future(awaitable)
        stopped = asyncio.create_task(self.context.cancelled.wait())
        try:
            await asyncio.wait({work, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if stopped.done() and self.context.cancelled.is_set():
                raise asyncio.CancelledError
            return await work
        finally:
            for task in (work, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, stopped, return_exceptions=True)

    async def next_input(self):
        if self.state == "closed":
            return None
        value = await self._wait(self._inputs.get())
        if value is None:
            return None
        async with self._lock:
            self.current = value
            self.state = "generating"
            await self.context._emit("turn.started", self._turn())
        return value

    def _turn(self):
        if self.current is None:
            raise ProtocolError("NO_ACTIVE_TURN", "Conversation has no active input")
        return {
            "turn_id": self.current["turn_id"],
            "input_id": self.current["input_id"],
            "revision": self.revision,
        }

    def _check_revision(self, payload):
        if payload["expected_revision"] != self.revision:
            raise ProtocolError("REVISION_CONFLICT", "Conversation revision changed")

    async def control(self, name, payload):
        validate(COMMANDS[name], payload)
        async with self._lock:
            identity, content_hash = (
                payload["input_id"],
                signature({"command": name, "payload": payload}),
            )
            previous = self._receipts.get(identity)
            if previous:
                if previous[0] != content_hash:
                    raise ProtocolError("INPUT_CONFLICT", "Input identity has different content")
                return previous[1]
            if name != "session.close" and len(self._receipts) >= self.config.get(
                "max_inputs", 1024
            ):
                raise ProtocolError("INPUT_LIMIT", "Conversation input ledger is full")
            self._check_revision(payload)
            if name == "turn.submit":
                if self.state != "waiting_input":
                    raise ProtocolError("INPUT_BUSY", "Conversation is not waiting for input")
                if payload["turn_id"] in self._turn_ids:
                    raise ProtocolError(
                        "TURN_CONFLICT", "Conversation turn identity was already used"
                    )
                self._turn_ids.add(payload["turn_id"])
                self.revision += 1
                self.state = "queued"
                self._inputs.put_nowait(payload)
                receipt = {"accepted": True, "revision": self.revision, "input_id": identity}
            elif name == "tool.respond":
                receipt = self._respond(payload)
            else:
                if self.state != "waiting_input":
                    raise ProtocolError(
                        "INPUT_BUSY", "Close requires an idle conversation; abort stops active work"
                    )
                self.revision += 1
                self.state = "closed"
                self.close_reason = payload["reason"]
                self._inputs.put_nowait(None)
                await self.context._emit(
                    "session.end", {"revision": self.revision, "reason": self.close_reason}
                )
                receipt = {"accepted": True, "revision": self.revision, "input_id": identity}
            self._receipts[identity] = (content_hash, receipt)
            return receipt

    def _respond(self, payload):
        pending = self._pending
        if self.state != "waiting_tool" or pending is None:
            raise ProtocolError("NO_PENDING_TOOLS", "Conversation is not waiting for tools")
        if (
            payload["turn_id"] != self.current["turn_id"]
            or payload["batch_id"] != pending["batch_id"]
        ):
            raise ProtocolError("TOOL_BATCH_CONFLICT", "Tool batch does not match the active step")
        identities = [item["call_id"] for item in payload["results"]]
        if len(set(identities)) != len(identities) or any(
            identity not in pending["calls"] for identity in identities
        ):
            raise ProtocolError(
                "UNKNOWN_TOOL_CALL", "Tool result identities do not match the active batch"
            )
        # Validate the complete command before fulfilling any native effect.
        for result in payload["results"]:
            previous = pending["results"].get(result["call_id"])
            if previous is not None and signature(previous) != signature(result):
                raise ProtocolError("TOOL_RESULT_CONFLICT", "A tool result was already fixed")
        for result in payload["results"]:
            pending["results"][result["call_id"]] = result
        if len(pending["results"]) == len(pending["calls"]):
            self.state = "generating"
            pending["future"].set_result(
                [pending["results"][identity] for identity in pending["calls"]]
            )
        return {"accepted": True, "revision": self.revision, "input_id": payload["input_id"]}

    async def request_tools(self, message_id, batch_id, calls, *, content=""):
        if self.mode != "external_steps":
            raise ProtocolError(
                "UNSUPPORTED_CAPABILITY", "External tool execution was not selected"
            )
        async with self._lock:
            if self.state != "generating":
                raise ProtocolError("INPUT_BUSY", "Conversation already has pending work")
            data = {**self._turn(), "message_id": message_id, "batch_id": batch_id, "calls": calls}
            validate("conversation_tool_request", data)
            identities = [call["call_id"] for call in calls]
            if len(set(identities)) != len(identities) or batch_id in self._batches:
                raise ProtocolError("TOOL_BATCH_CONFLICT", "Tool batch identity was already used")
            if self._tool_count + len(calls) > self.config.get("max_tool_calls", 4096):
                raise ProtocolError("TOOL_LIMIT", "Conversation tool ledger is full")
            self._tool_count += len(calls)
            self._batches.add(batch_id)
            future = asyncio.get_running_loop().create_future()
            self._pending = {
                "batch_id": batch_id,
                "calls": identities,
                "results": {},
                "future": future,
            }
            self.state = "waiting_tool"
            await self.context._emit(
                "turn.output",
                {
                    **self._turn(),
                    "message_id": message_id,
                    "output": {"role": "assistant", "content": content, "tool_calls": calls},
                },
            )
            await self.context._emit("tool.request", data)
        try:
            return await self._wait(future)
        finally:
            self._pending = None

    async def complete_turn(self, message_id, content):
        async with self._lock:
            if self.state != "generating" or self._pending is not None:
                raise ProtocolError("INPUT_BUSY", "A turn cannot finish with pending work")
            turn = self._turn()
            await self.context._emit(
                "turn.output",
                {
                    **turn,
                    "message_id": message_id,
                    "output": {"role": "assistant", "content": content},
                },
            )
            await self.context._emit("turn.completed", turn)
            self.current = None
            self.state = "waiting_input"
            await self.context._emit("session.waiting_input", {"revision": self.revision})
