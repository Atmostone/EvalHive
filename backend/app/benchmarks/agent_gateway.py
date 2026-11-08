"""Translate declared SDK agent calls into one host-owned Harness execution."""

import asyncio
import uuid

from evalhive_benchmark import ProtocolError

from app.benchmarks.async_control import bounded, critical


class AgentGateway:
    def __init__(self, calls, executor):
        self.calls, self.executor = calls, executor

    def configure(self, prepared):
        self.executor.configure_access(prepared)

    @staticmethod
    def _unwrap(receipt):
        if receipt["ok"]:
            return receipt["result"]
        error = receipt["error"]
        raise ProtocolError(error["code"], error["message"], error["kind"])

    async def replay(self, frame):
        ticket = await self.calls.begin(frame, replay_only=True)
        return self._unwrap(ticket.replay)

    async def _execute(self, frame, ticket):
        params = frame["params"]
        operation, payload = params["operation"], params["payload"]
        if operation in {"run", "open"}:
            if (operation == "run") != (self.executor.mode == "autonomous"):
                raise ProtocolError(
                    "AGENT_MODE_CONFLICT",
                    "Agent call differs from the frozen interaction mode",
                    "configuration",
                )
            await self.executor.prepare(payload.get("initial_history"))
            await self.calls.bind_attempt(ticket.id)
            await self.executor.start()
            if operation == "run":
                await self.executor.wait()
                return await self.calls.completion()
            return {"session_id": str(ticket.session_id), "attempt_id": str(ticket.session_id)}
        if operation == "control":
            command_id = uuid.uuid5(ticket.id, "harness.control")
            result = await self.executor.control(command_id, payload["name"], payload["payload"])
            return {
                "session_id": str(ticket.session_id),
                "command_id": str(command_id),
                "result": result,
            }
        if operation == "close":
            await self.executor.close(
                uuid.uuid5(ticket.id, "harness.close"), payload.get("reason", "Benchmark finished")
            )
            await self.executor.wait()
            return await self.calls.completion()
        deadline = asyncio.get_running_loop().time() + payload.get("wait_seconds", 0)
        while True:
            page = await self.calls.event_page(payload)
            remaining = deadline - asyncio.get_running_loop().time()
            if page["events"] or page["closed"] or remaining <= 0:
                return page
            await asyncio.sleep(min(0.1, remaining))

    async def handle(self, frame):
        ticket = await self.calls.begin(frame)
        if ticket.replay is not None:
            return self._unwrap(ticket.replay)
        try:
            result = await self._execute(frame, ticket)
            receipt = {
                "protocol_version": "1.0",
                "type": "response",
                "id": frame["id"],
                "ok": True,
                "result": result,
            }
            await self.calls.complete(ticket.id, receipt)
        except ProtocolError as error:
            # Native/provider details may contain private values. Preserve the
            # diagnostic category without exposing their exception text.
            receipt = {
                "protocol_version": "1.0",
                "type": "response",
                "id": frame["id"],
                "ok": False,
                "error": {
                    "code": error.code,
                    "kind": error.kind,
                    "message": "Agent operation failed",
                },
            }
            await self.calls.complete(ticket.id, receipt)
        except BaseException:
            # Cancelling an await cannot prove that a native effect did not run.
            await critical(bounded(self.calls.uncertain(ticket.id), 3))
            raise
        return self._unwrap(receipt)

    async def cancel(self):
        await self.executor.cancel()

    async def outcome(self, reference):
        return await self.calls.outcome(reference)

    async def reconcile(self):
        return await self.calls.reconcile()
