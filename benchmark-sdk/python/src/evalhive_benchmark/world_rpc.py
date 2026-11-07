"""Private, bounded operation control for a persistent native service thread."""

from __future__ import annotations

import queue
import threading
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy

from evalhive_benchmark import ProtocolError, validate

from .world_wire import MAX_BYTES as MAX_BYTES
from .world_wire import WorldError, encoded, monotonic

MAX_OPERATIONS = 128
MAX_RECEIPTS = 1024


class World:
    """Keep native state on one thread while scoped broker work is pending."""

    def __init__(self, factory, *, deadline_seconds=86400):
        self.factory, self.scope = factory, None
        self.deadline = monotonic() + deadline_seconds
        self.operations, self.receipts = {}, OrderedDict()
        self.queue, self.lock = queue.Queue(MAX_OPERATIONS), threading.RLock()
        self.effects, self.effect_sequence = {}, 0
        self.sealed = False
        self.closing = threading.Event()
        self.thread = threading.Thread(target=self._run, name="native-service", daemon=True)
        self.thread.start()

    def _run(self):
        service = None
        while not self.closing.is_set():
            item = self.queue.get()
            if item is None:
                break
            operation, method, payload = item
            try:
                if service is None:
                    service = self.factory(self.effect)
                value = service(method, payload)
                encoded(value)
                with self.lock:
                    operation.update(state="completed", result=deepcopy(value))
            except Exception as error:
                with self.lock:
                    operation.update(
                        state="failed",
                        error={"code": "NATIVE_SERVICE_FAILED", "exception": type(error).__name__},
                    )

    def effect(self, operation, payload):
        """A native transport call waits for its scoped SDK broker receipt."""
        encoded(payload)
        with self.lock:
            if self.sealed or self.closing.is_set() or monotonic() >= self.deadline:
                raise WorldError("Native service is stopping")
            if (
                len(self.effects) >= MAX_RECEIPTS
                or sum(not item["future"].done() for item in self.effects.values())
                >= MAX_OPERATIONS
            ):
                raise WorldError("Native broker effects exceed their pending budget")
            identity = "effect." + str(self.effect_sequence)
            self.effect_sequence += 1
            future = Future()
            self.effects[identity] = {
                "operation": operation,
                "payload": deepcopy(payload),
                "future": future,
            }
        remaining = self.deadline - monotonic()
        if self.closing.is_set() or remaining <= 0:
            raise WorldError("Native service is stopping")
        return future.result(timeout=remaining)

    def request(self, request):
        encoded(request)
        if not isinstance(request, dict) or set(request) != {"id", "scope", "operation", "payload"}:
            raise WorldError("Native operation shape is invalid")
        identity, scope, method, payload = (
            request[name] for name in ("id", "scope", "operation", "payload")
        )
        if (
            not isinstance(identity, str)
            or not 1 <= len(identity) <= 200
            or not isinstance(payload, dict)
        ):
            raise WorldError("Native operation identity or payload is invalid")
        try:
            validate("scope", scope)
        except ProtocolError:
            raise WorldError("Native operation scope is invalid") from None
        if method == "begin" and (
            set(payload) != {"method", "params"}
            or payload["method"] not in {"prepare", "capture", "verify", "cleanup"}
            or not isinstance(payload["params"], dict)
        ):
            raise WorldError("Native service method is invalid")
        signature = encoded(request)
        with self.lock:
            if self.scope is None:
                if method != "finalize" and (
                    method != "begin" or payload.get("method") != "prepare"
                ):
                    raise WorldError("Native service must be bound by preparation")
                self.scope = deepcopy(scope)
            if scope != self.scope:
                raise WorldError("Native operation belongs to another scope")
            previous = self.receipts.get(identity)
            if previous is not None:
                if previous[0] != signature:
                    raise WorldError("Native request identity was reused")
                return deepcopy(previous[1])
            if method != "poll" and len(self.receipts) >= MAX_RECEIPTS:
                raise WorldError("Native operation receipt budget is exhausted")
            finalizing = method == "finalize" or (
                method == "poll" and payload.get("operation_id") == "host.finalize"
            )
            if self.closing.is_set() or (monotonic() >= self.deadline and not finalizing):
                raise WorldError("Native service is stopping")
            if method == "finalize":
                if identity != "host.finalize" or payload:
                    raise WorldError("Native finalization requires a stable host receipt")
                self.sealed = True
                for item in self.effects.values():
                    if not item["future"].done():
                        item["future"].set_exception(WorldError("Native service is finalizing"))
                self.operations[identity] = {"state": "pending"}
                self.queue.put_nowait((self.operations[identity], "cleanup", {}))
                result = {"operation_id": identity}
            elif method == "begin":
                if self.sealed:
                    raise WorldError("Native service is sealed for finalization")
                if len(self.operations) >= MAX_OPERATIONS:
                    raise WorldError("Native operation budget is exhausted")
                operation = {"state": "pending"}
                self.operations[identity] = operation
                self.queue.put_nowait((operation, payload["method"], deepcopy(payload["params"])))
                result = {"operation_id": identity}
            elif method == "poll":
                if (
                    set(payload) != {"operation_id"}
                    or payload["operation_id"] not in self.operations
                ):
                    raise WorldError("Native operation is unavailable")
                result = deepcopy(self.operations[payload["operation_id"]])
                result["effects"] = [
                    {
                        "id": name,
                        "operation": item["operation"],
                        "payload": deepcopy(item["payload"]),
                    }
                    for name, item in self.effects.items()
                    if not item["future"].done()
                ]
            elif method == "resume":
                if (
                    set(payload) != {"effect_id", "result"}
                    or payload["effect_id"] not in self.effects
                ):
                    raise WorldError("Native broker effect is unavailable")
                future = self.effects[payload["effect_id"]]["future"]
                if future.done():
                    if future.result() != payload["result"]:
                        raise WorldError("Native broker effect receipt conflicts")
                else:
                    future.set_result(deepcopy(payload["result"]))
                result = {"accepted": True}
            else:
                raise WorldError("Native operation is unsupported")
            encoded(result)
            # Polling is a read and must observe progress when retried. Only
            # mutating commands consume durable in-process receipt slots.
            if method != "poll":
                self.receipts[identity] = signature, deepcopy(result)
            return result

    def close(self, *, timeout=1):
        self.closing.set()
        with self.lock:
            for item in self.effects.values():
                if not item["future"].done():
                    item["future"].set_exception(WorldError("Native service was cancelled"))
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        self.thread.join(timeout=timeout)
        return not self.thread.is_alive()
