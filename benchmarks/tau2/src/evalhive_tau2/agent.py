"""Native HalfDuplexAgent delegation through a harness-independent session."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from copy import deepcopy

from evalhive_benchmark import ProtocolError

from .native import load_domain


class ExternalSession:
    def __init__(self, context, scope, *, deadline, schemas):
        self.context, self.scope, self.deadline = context, deepcopy(scope), deadline
        self.schemas = deepcopy(schemas)
        self.session_id, self.pending = None, None
        self.cursor, self.sequence, self.revision = 0, 0, 0
        self.events, self.mapping = deque(), {}
        self.closed, self.waiting = False, False
        self.calls = set()

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if self.context.cancelled.is_set() or remaining <= 0:
            raise ProtocolError(
                "NATIVE_AGENT_STOPPED", "Native participant was cancelled or expired"
            )
        return min(remaining, 86400)

    async def call(self, operation, payload):
        self.sequence += 1
        return await self.context.agent(
            self.scope,
            operation,
            payload,
            session_id=self.session_id,
            request_id="tau2.agent." + str(self.sequence),
            timeout=self.remaining(),
        )

    async def event(self):
        while not self.events:
            if self.closed:
                raise ProtocolError(
                    "NATIVE_AGENT_CLOSED", "Harness ended before the native participant"
                )
            page = await self.call("events", {"cursor": self.cursor, "wait_seconds": 1})
            events = page["events"]
            if page["cursor"] != self.cursor + len(events):
                raise ProtocolError(
                    "NATIVE_AGENT_CURSOR", "Harness event cursor skipped native evidence"
                )
            self.cursor, self.closed = page["cursor"], page["closed"]
            self.events.extend(events)
        return self.events.popleft()

    def mapped(self, event):
        data = event.get("data", {})
        if event["event"] != "native" or data.get("name") != "external_tools.mapping":
            return
        rows = data["payload"]["tools"]
        mapping = {row["exposed_name"]: row["native_name"] for row in rows}
        if (
            len(mapping) != len(rows)
            or len(mapping) != len(self.schemas)
            or set(mapping.values()) != set(self.schemas)
        ):
            raise ProtocolError(
                "NATIVE_TOOL_MAPPING", "Harness tool mapping differs from the native catalog"
            )
        for row in rows:
            if row["schema_sha256"] != self.schemas[row["native_name"]]:
                raise ProtocolError(
                    "NATIVE_TOOL_MAPPING", "Harness discovered a changed native tool schema"
                )
        if self.mapping and mapping != self.mapping:
            raise ProtocolError(
                "NATIVE_TOOL_MAPPING", "Harness tool mapping changed during a session"
            )
        self.mapping = mapping

    async def open(self, history):
        if self.session_id is not None:
            return
        result = await self.call("open", {"initial_history": history})
        self.session_id = result["session_id"]
        idle = False
        while True:
            event = await self.event()
            self.mapped(event)
            if event["event"] == "session.waiting_input" and event["data"]["revision"] == 0:
                idle = True
            if idle and (not self.schemas or self.mapping):
                self.waiting = True
                return

    async def control(self, name, payload):
        result = await self.call("control", {"name": name, "payload": payload})
        receipt = result.get("result", {})
        if not receipt.get("ok") or not receipt.get("result", {}).get("accepted"):
            raise ProtocolError(
                "NATIVE_AGENT_INPUT", "Harness rejected the native participant input"
            )
        self.revision = receipt["result"]["revision"]

    async def exchange(self, message, history):
        await self.open(history)
        if message["role"] == "user":
            if not self.waiting or self.pending is not None:
                raise ProtocolError(
                    "NATIVE_AGENT_ORDER", "Native user input interrupted pending tools"
                )
            turn = "tau2.turn." + str(self.revision + 1)
            await self.control(
                "turn.submit",
                {
                    "input_id": turn,
                    "turn_id": turn,
                    "expected_revision": self.revision,
                    "messages": [{"role": "user", "content": message["content"]}],
                },
            )
            self.waiting = False
        else:
            pending = self.pending
            results = message["results"]
            if (
                pending is None
                or {row["call_id"] for row in results}
                != {row["call_id"] for row in pending["calls"]}
                or len(results) != len(pending["calls"])
            ):
                raise ProtocolError(
                    "NATIVE_TOOL_RESULTS", "Native results differ from the pending tool batch"
                )
            await self.control(
                "tool.respond",
                {
                    "input_id": "tau2.results." + str(self.sequence),
                    "turn_id": pending["turn_id"],
                    "batch_id": pending["batch_id"],
                    "expected_revision": self.revision,
                    "results": results,
                },
            )
            self.pending = None
        output = None
        while True:
            event = await self.event()
            self.mapped(event)
            data = event.get("data", {})
            if event["event"] == "turn.output":
                output = data["output"]
            elif event["event"] == "tool.request":
                self.pending = data
                calls = []
                for call in data["calls"]:
                    if call["call_id"] in self.calls or call["name"] not in self.mapping:
                        raise ProtocolError(
                            "NATIVE_TOOL_MAPPING",
                            "Harness requested an undeclared or duplicate native call",
                        )
                    self.calls.add(call["call_id"])
                    calls.append(
                        {
                            "id": call["call_id"],
                            "name": self.mapping[call["name"]],
                            "arguments": deepcopy(call["arguments"]),
                            "requestor": "assistant",
                        }
                    )
                return {"content": (output or {}).get("content") or None, "tool_calls": calls}
            elif event["event"] == "session.waiting_input":
                if output is None or output.get("tool_calls") or data["revision"] != self.revision:
                    raise ProtocolError(
                        "NATIVE_AGENT_ORDER", "Harness ended a native turn without its output"
                    )
                self.waiting = True
                return {"content": output["content"], "tool_calls": None}

    async def close(self):
        if self.session_id is None:
            return None
        if not self.waiting and not self.closed:
            result = await self.call("control", {"name": "abort", "payload": {}})
            if not result.get("result", {}).get("ok"):
                raise ProtocolError(
                    "NATIVE_AGENT_STOP", "Harness did not acknowledge native termination"
                )
            self.events.clear()
            while not self.closed:
                page = await self.call("events", {"cursor": self.cursor, "wait_seconds": 1})
                if page["cursor"] != self.cursor + len(page["events"]):
                    raise ProtocolError(
                        "NATIVE_AGENT_CURSOR", "Harness event cursor skipped native evidence"
                    )
                self.cursor, self.closed = page["cursor"], page["closed"]
        return await self.call("close", {"reason": "Native tau2 simulation finished"})


def create_agent(root, domain, tools, domain_policy, exchange):
    load_domain(root, domain)
    from tau2.agent.base_agent import HalfDuplexAgent, is_valid_agent_history_message
    from tau2.data_model.message import AssistantMessage, MultiToolMessage, ToolMessage, UserMessage

    def history_message(message):
        result = {"role": message.role, "content": message.content or ""}
        if isinstance(message, ToolMessage):
            result.update(call_id=message.id, is_error=message.error)
        elif isinstance(message, AssistantMessage) and message.tool_calls:
            result["tool_calls"] = [
                {"call_id": call.id, "name": call.name, "arguments": deepcopy(call.arguments)}
                for call in message.tool_calls
            ]
        return result

    class HarnessAgent(HalfDuplexAgent):
        @staticmethod
        def public_history(state):
            return [history_message(item) for item in state]

        def get_init_state(self, message_history=None):
            history = deepcopy(message_history or [])
            if not all(is_valid_agent_history_message(message) for message in history):
                raise ProtocolError(
                    "NATIVE_HISTORY", "Native agent received invalid initial history"
                )
            return history

        def generate_next_message(self, message, state):
            if (
                isinstance(message, UserMessage)
                and not message.is_tool_call()
                and not message.is_audio
            ):
                value = {"role": "user", "content": message.content or ""}
                incoming = [message]
            else:
                incoming = (
                    message.tool_messages if isinstance(message, MultiToolMessage) else [message]
                )
                if not all(
                    isinstance(item, ToolMessage) and item.requestor == "assistant"
                    for item in incoming
                ):
                    raise ProtocolError(
                        "NATIVE_INPUT", "Native agent input is outside the half-duplex baseline"
                    )
                value = {
                    "role": "tool",
                    "results": [
                        {"call_id": item.id, "content": item.content or "", "is_error": item.error}
                        for item in incoming
                    ],
                }
            answer = exchange(value, [history_message(item) for item in state])
            output = AssistantMessage(role="assistant", **answer)
            state.extend(deepcopy(incoming))
            state.append(output)
            return output, state

    return HarnessAgent(tools=tools, domain_policy=domain_policy)


def thread_exchange(session, loop):
    def exchange(message, history):
        future = asyncio.run_coroutine_threadsafe(session.exchange(message, history), loop)
        try:
            return future.result(timeout=session.remaining())
        except BaseException:
            future.cancel()
            raise

    return exchange
