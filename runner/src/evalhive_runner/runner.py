"""One attempt: connector supervision, durable event delivery and command receipts."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from evalhive_harness import ProtocolError, error_response, response, validate, validate_run

from .api import RetryableDeliveryError
from .redaction import Redactor
from .spool import EventSpool, SpoolFull


@dataclass(frozen=True)
class RunnerPolicy:
    heartbeat_seconds: float = 2
    retry_seconds: float = 0.25
    flush_seconds: float = 30
    shutdown_seconds: float = 3
    command_seconds: float = 10
    startup_seconds: float = 30
    finalization_seconds: float = 30


class AttemptRunner:
    def __init__(
        self,
        *,
        spec: dict,
        manifest: dict,
        connector,
        api,
        spool: EventSpool,
        redactor: Redactor | None = None,
        policy: RunnerPolicy | None = None,
        finish_hook: Callable[[dict, Callable[[str, dict], None]], Awaitable[dict]] | None = None,
    ):
        self.spec, self.manifest, self.connector, self.api, self.spool = (
            spec,
            manifest,
            connector,
            api,
            spool,
        )
        self.redactor, self.policy = redactor or Redactor(), policy or RunnerPolicy()
        self.finish_hook = finish_hook
        self._wake = asyncio.Event()
        self._done = False
        self._terminal = None
        self._fatal = None
        self._ready = asyncio.Event()
        self._abort_requested = asyncio.Event()
        self._terminal_received = asyncio.Event()
        self._cancelled = False
        self.redacted_records = 0
        self.connector_closed = False
        self._started_monotonic = None
        self._elapsed = None
        self._artifact_paths = set()

    def append(self, event):
        event = {"observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"), **event}
        clean = self.redactor.event(event)
        self.redacted_records += clean != event
        if clean["event"] == "run.finished":
            clean["data"]["metadata"] = {
                **clean["data"].get("metadata", {}),
                "runner_elapsed_seconds": self._elapsed,
                "elapsed_source": "runner_observation",
                "redacted_records": self.redacted_records,
            }
        self.spool.append(clean)
        self._wake.set()

    def synthetic(self, name, data):
        return {
            "protocol_version": "1.0",
            "type": "event",
            "event_id": str(uuid.uuid4()),
            "attempt_id": self.spec["attempt_id"],
            "seq": self.spool.next_seq,
            "source": "runner",
            "event": name,
            "data": data,
        }

    async def _consume(self):
        expected = self.spool.next_seq
        async for event in self.connector.events():
            if event["attempt_id"] != self.spec["attempt_id"] or event["seq"] != expected:
                raise ProtocolError(
                    "INVALID_STREAM", "Connector event scope or sequence is invalid"
                )
            expected += 1
            # Observation time is assigned outside the harness, before retries
            # or artifact collection can change the apparent execution duration.
            event = {**event, "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z")}
            if event["event"] == "run.started":
                self._started_monotonic = time.monotonic()
            if event["event"] == "run.finished":
                if self._started_monotonic is not None:
                    self._elapsed = time.monotonic() - self._started_monotonic
                self._terminal = event
                self._terminal_received.set()
                return
            if event["event"] == "artifact":
                self._artifact_paths.add(event["data"]["path"])
                if len(self._artifact_paths) > 10000:
                    raise ProtocolError("ARTIFACT_LIMIT", "Connector announced too many artifacts")
                event = {
                    **event,
                    "data": {
                        **event["data"],
                        "metadata": {
                            "delivery": "announced",
                            "native_metadata": event["data"].get("metadata", {}),
                        },
                    },
                }
            self.append(event)
            if event["event"] == "interaction.request":
                kind = event["data"]["kind"]
                if (
                    self.manifest.get("capabilities", {}).get(kind) not in ("native", "emulated")
                    or kind not in self.manifest.get("commands", {})
                ):
                    raise ProtocolError(
                        "UNSUPPORTED_INTERACTION", "Connector requested unsupported interaction"
                    )
        raise ProtocolError("CONNECTOR_EXIT", "Connector exited without a terminal event")

    async def _deliver(self):
        delay = self.policy.retry_seconds
        try:
            while not self._done or self.spool.pending_count:
                batch = self.spool.batch()
                if not batch:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), 0.2)
                    except TimeoutError:
                        pass
                    continue
                try:
                    receipt = await self.api.events(batch)
                    through = receipt.get("acked_through")
                    if (
                        type(through) is not int
                        or through < batch[-1]["seq"]
                        or receipt.get("next_seq") != through + 1
                    ):
                        raise ProtocolError(
                            "INVALID_RECEIPT", "Attempt API returned an invalid acknowledgement"
                        )
                    self.spool.acknowledge(through)
                    delay = self.policy.retry_seconds
                except RetryableDeliveryError:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 5)
        except Exception as error:
            self._fatal = error
            raise

    async def _flush_command_receipts(self):
        for command_id, receipt in self.spool.completed_commands():
            await self.api.acknowledge(command_id, receipt)
            self.spool.command_acknowledged(command_id)

    async def _control(self, initial=None):
        while not self._done:
            try:
                await self._flush_command_receipts()
                heartbeat = initial if initial is not None else await self.api.heartbeat()
                initial = None
                if heartbeat.get("status") in ("lost", "success", "failed", "cancelled"):
                    if self._terminal is None:
                        raise ProtocolError("ATTEMPT_FINISHED", "Server ended the attempt")
                    return
                for command in heartbeat.get("commands", []):
                    command_id = command["id"]
                    cached = self.spool.command(command_id)
                    if cached is not None:
                        if cached[0] == "executing":
                            receipt = error_response(
                                command_id,
                                ProtocolError(
                                    "COMMAND_UNCERTAIN",
                                    "Command execution was interrupted; it was not replayed",
                                ),
                            )
                            self.spool.command_finished(command_id, receipt)
                        continue
                    self.spool.command_started(command_id)
                    if command["name"] == "abort":
                        self._cancelled = True
                        self._abort_requested.set()
                    await self._ready.wait()
                    try:
                        if command["name"] == "abort" and self.manifest["capabilities"].get(
                            "abort"
                        ) not in ("native", "emulated"):
                            result = {"accepted": True, "mechanism": "runtime"}
                        else:
                            result = await self.connector.request(
                                "control",
                                {
                                    "attempt_id": self.spec["attempt_id"],
                                    "command": command["name"],
                                    "payload": command.get("payload", {}),
                                },
                                timeout=self.policy.command_seconds,
                            )
                        receipt = response(command_id, result)
                    except ProtocolError as error:
                        receipt = error_response(command_id, error)
                    self.spool.command_finished(command_id, self.redactor.response(receipt))
                await self._flush_command_receipts()
            except RetryableDeliveryError:
                pass
            except Exception as error:
                self._fatal = error
                raise
            await asyncio.sleep(self.policy.heartbeat_seconds)

    async def _abort_watchdog(self):
        await self._abort_requested.wait()
        try:
            await asyncio.wait_for(self._terminal_received.wait(), self.policy.shutdown_seconds)
        except TimeoutError:
            raise ProtocolError(
                "ABORT_GRACE_EXPIRED", "Connector did not stop within the cancellation grace period"
            ) from None

    async def run(self) -> dict:
        validate_run(self.manifest, self.spec)
        if self.spool.next_seq:
            raise ProtocolError(
                "ATTEMPT_EXISTS", "A new connector cannot reuse an existing attempt journal"
            )
        self.spool.claim_run()
        initial = None
        try:
            initial = await self.api.heartbeat()
            if any(command["name"] == "abort" for command in initial.get("commands", [])):
                self._cancelled = True
                for command in initial["commands"]:
                    if command["name"] == "abort":
                        self.spool.command_started(command["id"])
                        self.spool.command_finished(
                            command["id"],
                            response(
                                command["id"], {"accepted": True, "mechanism": "before_start"}
                            ),
                        )
        except RetryableDeliveryError:
            pass
        delivery = asyncio.create_task(self._deliver())
        controls = asyncio.create_task(self._control(initial))
        watchdog = asyncio.create_task(self._abort_watchdog())
        consumer = asyncio.create_task(self._consume())
        failure = None
        try:
            described = await self.connector.request(
                "describe", timeout=self.policy.startup_seconds
            )
            for key in (
                "id",
                "version",
                "protocol_version",
                "config_schema",
                "capabilities",
                "commands",
            ):
                if described.get(key) != self.manifest.get(key):
                    raise ProtocolError(
                        "MANIFEST_MISMATCH",
                        "Worker does not match the registered connector contract",
                    )
            validation = await self.connector.request(
                "validate", {"run": self.spec}, timeout=self.policy.command_seconds
            )
            validate("validation_result", validation)
            applied = validation["effective_config"]
            validate_run(self.manifest, {**self.spec, "config": applied})
            if self.redactor.value(applied) != applied:
                raise ProtocolError("PRIVATE_CONFIGURATION", "Applied configuration contains private values")
            try:
                async with asyncio.timeout(self.policy.command_seconds):
                    while not self._cancelled:
                        try:
                            receipt = await self.api.prepare(applied)
                            if receipt.get("accepted") is not True:
                                raise ProtocolError("CONFIGURATION_REJECTED", "Applied configuration was not saved")
                            self.spec["config"] = applied
                            break
                        except RetryableDeliveryError:
                            await asyncio.sleep(self.policy.retry_seconds)
            except TimeoutError:
                raise ProtocolError("CONFIGURATION_UNAVAILABLE", "Applied configuration could not be saved before execution") from None
            if self._cancelled:
                raise ProtocolError(
                    "CANCELLED_BEFORE_START", "Run cancelled before harness execution"
                )
            await self.connector.request(
                "start", {"run": self.spec}, timeout=self.policy.command_seconds
            )
            self._ready.set()
            timeout = self.spec.get("limits", {}).get("wall_time_seconds", 3600)
            async with asyncio.timeout(timeout):
                done, _ = await asyncio.wait(
                    [consumer, delivery, controls, watchdog], return_when=asyncio.FIRST_COMPLETED
                )
                if consumer in done:
                    await consumer
                elif watchdog in done:
                    await watchdog
                    await consumer
                elif self._fatal is not None:
                    raise self._fatal
                else:
                    raise ProtocolError("RUNNER_STOPPED", "Runner service ended unexpectedly")
        except TimeoutError:
            failure = ProtocolError("DEADLINE", "Attempt reached its execution deadline")
            self._cancelled = True
            try:
                await self.connector.request(
                    "control",
                    {"attempt_id": self.spec["attempt_id"], "command": "abort"},
                    timeout=self.policy.shutdown_seconds,
                )
                await asyncio.wait_for(asyncio.shield(consumer), self.policy.shutdown_seconds)
            except Exception:
                pass
        except asyncio.CancelledError:
            failure = ProtocolError("RUNNER_INTERRUPTED", "Runner service was interrupted")
        except Exception as error:
            failure = (
                ProtocolError("SPOOL_FULL", str(error)) if isinstance(error, SpoolFull) else error
            )
        finally:
            self._ready.set()
            controls.cancel()
            watchdog.cancel()
            await asyncio.gather(controls, watchdog, return_exceptions=True)
            # Connector shutdown precedes artifact collection, including background children.
            try:
                await self.connector.close(grace=self.policy.shutdown_seconds)
                self.connector_closed = True
            except Exception:
                failure = ProtocolError(
                    "SHUTDOWN_FAILED", "Connector shutdown could not be confirmed"
                )
            if not consumer.done():
                consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

        terminal = self._terminal
        if terminal is None:
            if self.spool.next_seq == 0:
                self.append(self.synthetic("run.started", {"harness_started": False}))
            code = failure.code if isinstance(failure, ProtocolError) else "CONNECTOR_FAILED"
            message = (
                str(failure) if isinstance(failure, ProtocolError) else "Connector execution failed"
            )
            terminal = self.synthetic(
                "run.finished",
                {
                    "status": "failed",
                    "summary": message,
                    "error": {"code": code, "message": message},
                    "metadata": {"connector_terminal": "missing", "harness_started": self._started_monotonic is not None},
                },
            )
        if isinstance(failure, ProtocolError) and failure.code in ("DEADLINE", "SHUTDOWN_FAILED"):
            terminal = {
                **terminal,
                "source": "runner",
                "data": {
                    "status": "failed",
                    "summary": str(failure),
                    "error": failure.as_dict(),
                    "metadata": {
                        "connector_result": terminal["data"],
                        "termination_reason": "deadline"
                        if failure.code == "DEADLINE"
                        else "shutdown_failed",
                    },
                },
            }
        elif self._cancelled:
            terminal = {
                **terminal,
                "source": "runner",
                "data": {
                    "status": "cancelled",
                    "summary": "Run cancelled",
                    "metadata": {
                        "connector_result": terminal["data"],
                        "termination_reason": "abort",
                    },
                },
            }
        elif failure is not None and self._terminal is not None:
            error = (
                failure
                if isinstance(failure, ProtocolError)
                else ProtocolError("RUNNER_ERROR", "Runner execution failed")
            )
            terminal = {
                **terminal,
                "source": "runner",
                "data": {
                    "status": "failed",
                    "summary": str(error),
                    "error": error.as_dict(),
                    "metadata": {"connector_result": terminal["data"]},
                },
            }
        if self.finish_hook is not None:
            terminal = {
                **terminal,
                "data": {
                    **terminal["data"],
                    "metadata": {
                        **terminal["data"].get("metadata", {}),
                        "declared_artifacts": sorted(self._artifact_paths),
                    },
                },
            }
            try:

                def emit(name, data):
                    if name in ("run.started", "run.finished"):
                        raise ValueError("Finalization cannot emit lifecycle events")
                    self.append(self.synthetic(name, data))

                terminal = {
                    **terminal,
                    "data": await asyncio.wait_for(
                        self.finish_hook(terminal["data"], emit), self.policy.finalization_seconds
                    ),
                }
            except Exception:
                terminal = {
                    **terminal,
                    "source": "runner",
                    "data": {
                        "status": "failed",
                        "summary": "Artifact collection failed",
                        "error": {
                            "code": "ARTIFACT_DELIVERY",
                            "message": "Artifact collection failed",
                        },
                        "metadata": {"connector_result": terminal["data"]},
                    },
                }
        terminal = {
            **terminal,
            "seq": self.spool.next_seq,
            "data": {
                **terminal["data"],
                "metadata": {
                    **terminal["data"].get("metadata", {}),
                    "runner_elapsed_seconds": self._elapsed,
                    "elapsed_source": "runner_observation",
                    "redacted_records": self.redacted_records,
                },
            },
        }
        try:
            self.append(terminal)
        except (ProtocolError, SpoolFull):
            # A large native result can fit the wire before finalization adds
            # metadata. Preserve preceding evidence and commit a bounded failure.
            terminal = self.synthetic(
                "run.finished",
                {
                    "status": "failed",
                    "summary": "Final result could not fit the journal",
                    "error": {
                        "code": "TERMINAL_RECORD_LIMIT",
                        "message": "Final result exceeds the record budget",
                    },
                    "metadata": {"connector_terminal": "not_preserved"},
                },
            )
            self.append(terminal)
        self._done = True
        self._wake.set()
        try:
            await asyncio.wait_for(delivery, self.policy.flush_seconds)
            await self._flush_command_receipts()
        except (TimeoutError, RetryableDeliveryError):
            # The persistent spool is recovered by the server after reconnection.
            return {"status": "delivery_pending", "pending_events": self.spool.pending_count}
        return {"status": terminal["data"]["status"], "pending_events": self.spool.pending_count}
