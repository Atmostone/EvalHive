"""Durable commands and ordered event barriers for one fresh worker transport."""

from __future__ import annotations

import asyncio
import math
import uuid
from contextlib import suppress

from evalhive_benchmark import ProtocolError, command
from evalhive_benchmark.peer import identity

from app.benchmarks.commands import CommandStore


class WorkerChannel:
    def __init__(self, trials, owner, actor_id, client, *, command_limit=16):
        if type(command_limit) is not int or not 1 <= command_limit <= 64:
            raise ValueError("Worker channel requires 1 to 64 concurrent commands")
        self.trials, self.owner, self.actor_id, self.client = trials, owner, actor_id, client
        self.commands = CommandStore(trials, owner, actor_id)
        self.command_limit, self.active = command_limit, {}
        self.persisted, self.event_floor = 0, -1
        self.condition, self.event_error = asyncio.Condition(), None
        self.closing = False
        self.pumping = asyncio.create_task(self._events())

    @classmethod
    async def attach(cls, trials, owner, actor_id, client, **options):
        """Bind a fresh transport/actor. Lost native sessions are not resumed."""
        await trials.register_stream(owner, actor_id)
        return cls(trials, owner, actor_id, client, **options)

    async def _events(self):
        try:
            async for event in self.client.events():
                accepted = await self.trials.ingest(self.owner, self.actor_id, [event])
                async with self.condition:
                    self.persisted += 1
                    self.event_floor = accepted["accepted_through"]
                    self.client.queue.task_done()
                    self.condition.notify_all()
        except Exception as error:
            self.event_error = error
        finally:
            async with self.condition:
                self.condition.notify_all()

    async def _barrier(self, count, timeout):
        async with asyncio.timeout(timeout):
            async with self.condition:
                while self.persisted < count:
                    if self.event_error is not None or self.pumping.done():
                        raise ProtocolError(
                            "EVENT_DELIVERY_FAILED",
                            "Worker event delivery is incomplete",
                            "recording",
                        )
                    await self.condition.wait()
                if self.event_error is not None:
                    raise ProtocolError(
                        "EVENT_DELIVERY_FAILED", "Worker event delivery failed", "recording"
                    )

    async def invoke(self, method, params=None, *, request_id=None, timeout=30):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 86400
        ):
            raise ValueError("Worker command timeout must be between 0 and 86400 seconds")
        if self.closing and method != "shutdown":
            raise ProtocolError("TRANSPORT_CLOSED", "Worker channel is closing")
        frame = command(request_id or uuid.uuid4().hex, method, params)
        signature = identity(frame)
        previous = self.active.get(frame["id"])
        if previous is not None:
            if previous[0] != signature:
                raise ProtocolError(
                    "COMMAND_CONFLICT", "Command ID has different content", "configuration"
                )
            return await asyncio.shield(previous[1])
        if len(self.active) >= self.command_limit:
            raise ProtocolError("PENDING_LIMIT", "Worker command limit reached")
        task = asyncio.create_task(self._invoke(frame, timeout))
        self.active[frame["id"]] = (signature, task)

        def done(completed):
            if self.active.get(frame["id"], (None, None))[1] is completed:
                self.active.pop(frame["id"], None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        return await asyncio.shield(task)

    async def _invoke(self, frame, timeout):
        ticket = await self.commands.begin(frame)
        if ticket.replay is None:
            try:
                sending = asyncio.create_task(
                    self.client.request_receipt(
                        frame["method"], frame["params"], request_id=frame["id"], timeout=timeout
                    )
                )
                try:
                    async with asyncio.timeout(timeout):
                        await asyncio.wait(
                            [sending, self.pumping], return_when=asyncio.FIRST_COMPLETED
                        )
                        if not sending.done():
                            raise ProtocolError(
                                "EVENT_DELIVERY_FAILED",
                                "Worker stream ended before its response",
                                "recording",
                            )
                        receipt = await sending
                finally:
                    if not sending.done():
                        sending.cancel()
                    await asyncio.gather(sending, return_exceptions=True)
                # All earlier ordered frames have already entered the SDK queue.
                # Capture a finite delivery count; future background events cannot
                # make this barrier wait forever.
                target = self.client.events_received
                await self.commands.receive(ticket.id, receipt)
                await self._barrier(target, timeout)
                ticket = await self.commands.complete(ticket.id, event_floor=self.event_floor)
            except BaseException as error:
                if isinstance(error, asyncio.CancelledError):
                    reason = "COMMAND_CANCELLED"
                elif (
                    isinstance(error, TimeoutError)
                    or isinstance(error, ProtocolError)
                    and error.code == "COMMAND_UNCERTAIN"
                ):
                    reason = "COMMAND_TIMEOUT"
                elif isinstance(error, ProtocolError) and error.code == "EVENT_DELIVERY_FAILED":
                    reason = "EVENT_DELIVERY_FAILED"
                else:
                    reason = "WORKER_DISCONNECTED"
                task = asyncio.create_task(self.commands.uncertain(ticket.id, code=reason))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                except Exception:
                    pass  # A persisted sent/received intent still forbids replay.
                raise
        # A duplicate completed command reads the durable receipt instead of
        # asking the worker to repeat an effect or resolve a retired tombstone.
        async with asyncio.timeout(min(timeout, 10)):
            await self.client.peer.acknowledge("command", ticket.request_id)
        await self.commands.acknowledge(ticket.id)
        if ticket.error_code == "INVALID_CONNECTOR_RESULT":
            raise ProtocolError(
                ticket.error_code, "Worker returned an invalid command result", "recording"
            )
        if not ticket.replay["ok"]:
            raise ProtocolError(
                "CONNECTOR_COMMAND_FAILED",
                "Benchmark worker command failed",
                ticket.replay["error"]["kind"],
            )
        return ticket.replay["result"]

    async def cancel_pending(self):
        pending = [value[1] for value in self.active.values()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def close(self, *, grace=3):
        self.closing = True
        await self.cancel_pending()
        try:
            if not self.client.peer.closed and self.event_error is None:
                with suppress(ProtocolError, TimeoutError):
                    await self.invoke("shutdown", timeout=grace)
        finally:
            await self.client.close(grace, shutdown=False)
            await self.pumping
        if self.event_error is not None:
            raise ProtocolError(
                "EVENT_DELIVERY_FAILED", "Worker stream did not close cleanly", "recording"
            )
        await self.trials.seal_stream(self.owner, self.actor_id, self.event_floor)
