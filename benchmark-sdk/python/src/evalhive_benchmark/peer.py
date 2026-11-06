"""Bounded bidirectional RPC; the coordinator owns durable recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterable, Awaitable, Callable

from .protocol import ProtocolError, decode_line, encode_line


def identity(value):
    """Compare JSON content independently of object insertion order."""

    def normalize(item):
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, dict):
            return {key: normalize(child) for key, child in item.items()}
        return item

    value = normalize(value)
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class Peer:
    def __init__(
        self,
        write: Callable[[bytes], Awaitable[None]],
        *,
        handler=None,
        replay_handler=None,
        request_type="command",
        event_handler=None,
        max_pending=64,
        max_active=16,
        receipt_limit=256,
        tombstone_limit=16384,
    ):
        if any(
            type(n) is not int or n < 1
            for n in (max_pending, max_active, receipt_limit, tombstone_limit)
        ):
            raise ValueError("RPC limits must be positive integers")
        self.write = write
        self.handler = handler
        self.replay_handler = replay_handler
        self.request_types = (
            (request_type,) if isinstance(request_type, str) else tuple(request_type)
        )
        if not self.request_types or any(
            kind not in {"command", "broker_request"} for kind in self.request_types
        ):
            raise ValueError("Invalid inbound request types")
        self.event_handler = event_handler
        self.max_pending, self.max_active, self.receipt_limit = (
            max_pending,
            max_active,
            receipt_limit,
        )
        self.tombstone_limit = tombstone_limit
        self.unacknowledged = 0
        self.pending = {}
        self.pending_receipts = set()
        self.receipts = {}
        self.active = set()
        self.lock = asyncio.Lock()
        self.closed = False

    async def send(self, value):
        frame = encode_line(value)
        async with self.lock:
            try:
                await self.write(frame)
            except (BrokenPipeError, ConnectionError):
                raise ProtocolError("TRANSPORT_CLOSED", "Peer transport is closed") from None

    async def request(self, value, *, timeout=30, receipt=False):
        if self.closed:
            raise ProtocolError("TRANSPORT_CLOSED", "Peer is closed")
        if len(self.pending) >= self.max_pending:
            raise ProtocolError("PENDING_LIMIT", "Pending request limit reached")
        encode_line(value)
        key = (value["type"], value["id"])
        if key in self.pending:
            raise ProtocolError("REQUEST_PENDING", "Request already awaits a response")
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        if receipt:
            self.pending_receipts.add(key)
        try:
            await self.send(value)
            try:
                async with asyncio.timeout(timeout):
                    return await future
            except TimeoutError:
                raise ProtocolError(
                    "COMMAND_UNCERTAIN", "Request timed out; its outcome is unknown"
                ) from None
        finally:
            self.pending.pop(key, None)
            self.pending_receipts.discard(key)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def acknowledge(self, request_type, request_id):
        """Call only after the complete receipt has been durably saved by the caller."""
        await self.send(
            {
                "protocol_version": "1.0",
                "type": "receipt_ack",
                "id": request_id,
                "request_type": request_type,
            }
        )

    async def _answer(self, value, *, handler=None):
        response_type = "broker_response" if value["type"] == "broker_request" else "response"
        base = {"protocol_version": "1.0", "type": response_type, "id": value["id"]}
        try:
            result = await (handler or self.handler)(value)
            reply = {**base, "ok": True, "result": result}
            encode_line(reply)
            return reply
        except asyncio.CancelledError:
            return {
                **base,
                "ok": False,
                "error": {
                    "code": "CANCELLED",
                    "message": "Operation cancelled",
                    "kind": "execution",
                },
            }
        except ProtocolError as error:
            return {**base, "ok": False, "error": error.as_dict()}
        except Exception:
            return {
                **base,
                "ok": False,
                "error": {
                    "code": "CONNECTOR_ERROR",
                    "message": "Connector operation failed",
                    "kind": "execution",
                },
            }

    async def _dispatch(self, value):
        key = (value["type"], value["id"])
        signature = identity(value)
        previous = self.receipts.get(key)
        if previous:
            if previous[0] != signature:
                reply = {
                    "protocol_version": "1.0",
                    "type": "broker_response" if value["type"] == "broker_request" else "response",
                    "id": value["id"],
                    "ok": False,
                    "error": {
                        "code": "COMMAND_CONFLICT",
                        "message": "Request identity has different content",
                        "kind": "configuration",
                    },
                }
                await self.send(reply)
                return
            if previous[1] is None:
                if self.replay_handler is not None:
                    await self.send(await self._answer(value, handler=self.replay_handler))
                    return
                await self.send(
                    {
                        "protocol_version": "1.0",
                        "type": "broker_response"
                        if value["type"] == "broker_request"
                        else "response",
                        "id": value["id"],
                        "ok": False,
                        "error": {
                            "code": "RECEIPT_RETIRED",
                            "message": "Read the acknowledged receipt from durable storage",
                            "kind": "uncertain",
                        },
                    }
                )
                return
            await self.send(await asyncio.shield(previous[1]))
            return
        full = (
            self.unacknowledged >= self.receipt_limit or len(self.receipts) >= self.tombstone_limit
        )
        if full or len(self.active) >= self.max_active:
            await self.send(
                {
                    "protocol_version": "1.0",
                    "type": "broker_response" if value["type"] == "broker_request" else "response",
                    "id": value["id"],
                    "ok": False,
                    "error": {
                        "code": "LEDGER_FULL" if full else "BUSY",
                        "message": "RPC capacity reached",
                        "kind": "transport",
                    },
                }
            )
            return
        task = asyncio.create_task(self._answer(value))
        self.receipts[key] = (signature, task)
        self.unacknowledged += 1
        self.active.add(task)
        task.add_done_callback(self.active.discard)
        await self.send(await asyncio.shield(task))

    async def read(self, lines: AsyncIterable[bytes]):
        deliveries = set()
        failure = ProtocolError("TRANSPORT_CLOSED", "Peer disconnected")
        try:
            async for line in lines:
                try:
                    value = decode_line(line)
                except ProtocolError as error:
                    if "command" in self.request_types and self.handler is not None:
                        await self.send(
                            {
                                "protocol_version": "1.0",
                                "type": "response",
                                "id": None,
                                "ok": False,
                                "error": error.as_dict(),
                            }
                        )
                    raise
                kind = value["type"]
                if kind == "receipt_ack":
                    key = (value["request_type"], value["id"])
                    previous = self.receipts.get(key)
                    if previous is None:
                        raise ProtocolError(
                            "UNKNOWN_RECEIPT", "Cannot acknowledge an unknown request"
                        )
                    if previous[1] is not None:
                        if not previous[1].done():
                            raise ProtocolError(
                                "ACK_BEFORE_RECEIPT", "Operation has not produced a receipt"
                            )
                        self.receipts[key] = (previous[0], None)
                        self.unacknowledged -= 1
                elif kind in ("response", "broker_response"):
                    key = ("command" if kind == "response" else "broker_request", value["id"])
                    future = self.pending.get(key)
                    if future is not None and not future.done():
                        if key in self.pending_receipts:
                            future.set_result(value)
                        elif value["ok"]:
                            future.set_result(value["result"])
                        else:
                            future.set_exception(
                                ProtocolError(
                                    value["error"]["code"],
                                    value["error"]["message"],
                                    value["error"]["kind"],
                                )
                            )
                    elif value["id"] is None:
                        raise ProtocolError(
                            value["error"]["code"],
                            value["error"]["message"],
                            value["error"]["kind"],
                        )
                elif kind == "event" and self.event_handler is not None:
                    await self.event_handler(value)
                elif kind in self.request_types and self.handler is not None:
                    # Bound duplicate deliveries as well as distinct active operations.
                    if len(deliveries) >= self.max_pending:
                        raise ProtocolError("PENDING_LIMIT", "Inbound request limit reached")
                    delivery = asyncio.create_task(self._dispatch(value))
                    deliveries.add(delivery)

                    def done(task):
                        deliveries.discard(task)
                        if not task.cancelled():
                            task.exception()

                    delivery.add_done_callback(done)
                else:
                    raise ProtocolError("INVALID_MESSAGE", "Unexpected message direction")
                await asyncio.sleep(0)
        except ProtocolError as error:
            failure = error
            raise
        except (ValueError, OSError):
            failure = ProtocolError("INVALID_FRAME", "Peer stream failed")
            raise failure from None
        finally:
            self.closed = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(failure)
            for task in [*self.active, *deliveries]:
                task.cancel()
            await asyncio.gather(*self.active, *deliveries, return_exceptions=True)


class Context:
    def __init__(self, peer: Peer):
        self.peer = peer
        self.cancelled = asyncio.Event()
        self.sequences = {}
        self.lock = asyncio.Lock()
        self.scope = None

    def bind(self, scope):
        if self.scope is not None and self.scope != scope:
            raise ProtocolError("SCOPE_CONFLICT", "A process belongs to one trial generation")
        self.scope = dict(scope)

    async def emit(self, scope, event, data, *, source="benchmark"):
        async with self.lock:
            key = (scope["workspace_id"], scope["trial_id"], scope["generation"])
            seq = self.sequences.get(key, 0)
            await self.peer.send(
                {
                    "protocol_version": "1.0",
                    "type": "event",
                    "id": uuid.uuid4().hex,
                    "scope": scope,
                    "seq": seq,
                    "source": source,
                    "event": event,
                    "data": data,
                }
            )
            self.sequences[key] = seq + 1

    async def broker(
        self,
        scope,
        provider,
        operation,
        payload,
        *,
        handle=None,
        request_id=None,
        timeout=30,
        durable_host=False,
    ):
        request = {
            "protocol_version": "1.0",
            "type": "broker_request",
            "id": request_id or uuid.uuid4().hex,
            "scope": scope,
            "provider": provider,
            "operation": operation,
            "payload": payload,
        }
        if handle is not None:
            request["handle"] = handle
        if not durable_host:
            return await self.peer.request(request, timeout=timeout)
        receipt = await self.peer.request(request, timeout=timeout, receipt=True)
        if receipt["ok"]:
            # Opt-in only: the host must commit successful broker receipts before
            # replying. Failed/uncertain replies retain their normal ledger entry.
            await self.peer.acknowledge("broker_request", request["id"])
            return receipt["result"]
        error = receipt["error"]
        raise ProtocolError(error["code"], error["message"], error["kind"])
