"""Automatic benchmark lifecycle; native orchestration remains in SDK workers."""

from __future__ import annotations

import asyncio
import math
from copy import deepcopy
from dataclasses import dataclass

from evalhive_benchmark import ProtocolError, validate, validate_spec

from app.benchmarks.async_control import bounded, cancel_tasks, consume

FENCED = {"STALE_GENERATION", "OWNER_EXPIRED", "TRIAL_NOT_FOUND", "TRIAL_FINISHED"}


@dataclass(frozen=True)
class DriverLimits:
    phase_seconds: float = 600
    stop_seconds: float = 3
    capture_seconds: float = 30
    cleanup_seconds: float = 60
    service_seconds: float = 5
    poll_seconds: float = 0.2
    heartbeat_seconds: float = 10

    def __post_init__(self):
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0.01 <= value <= 86400
            for value in self.__dict__.values()
        ):
            raise ValueError("Driver limits must be finite positive bounded durations")


class TrialDriver:
    def __init__(
        self,
        host,
        recovery,
        *,
        capture_providers,
        capture_validator,
        deadlines=(),
        agent_control=None,
        limits=None,
    ):
        self.host, self.store, self.owner = host, host.store, host.owner
        self.recovery, self.capture_providers = recovery, dict(capture_providers)
        if not callable(capture_validator):
            raise ValueError("A trusted host evidence validator is required")
        self.capture_validator = capture_validator
        self.deadlines, self.agent_control = tuple(deadlines), agent_control
        self.limits = limits or DriverLimits()
        if self.limits.heartbeat_seconds > self.store.lease_seconds / 3:
            raise ValueError("Heartbeat interval must leave two controller lease renewals")
        self.pending, self.services, self.used = set(), [], False

    async def _service(self, operation):
        try:
            await operation
            if not self.stopping.is_set():
                raise ProtocolError(
                    "SERVICE_STOPPED", "Controller service stopped unexpectedly", "execution"
                )
        except asyncio.CancelledError:
            if not self.stopping.is_set() and not self.failure.done():
                self.failure.set_result(
                    ProtocolError(
                        "SERVICE_STOPPED", "Controller service was cancelled", "execution"
                    )
                )
            raise
        except Exception as error:
            if not self.failure.done():
                self.failure.set_result(error)

    async def _pause(self, seconds):
        try:
            await asyncio.wait_for(self.stopping.wait(), seconds)
        except TimeoutError:
            pass

    async def _heartbeats(self):
        while not self.stopping.is_set():
            await bounded(self.store.heartbeat(self.owner), self.limits.service_seconds)
            await self._pause(self.limits.heartbeat_seconds)

    async def _control(self):
        while not self.stopping.is_set():
            state = await bounded(self.store.control(self.owner), self.limits.service_seconds)
            if state.phase != "cleaning" and not self.interruption.done():
                if state.cancelled:
                    self.interruption.set_result("CANCELLED")
                elif state.seconds_remaining <= 0:
                    self.interruption.set_result("TRIAL_DEADLINE")
            await self._pause(self.limits.poll_seconds)

    async def _deadline_result(self, result):
        await self.store.note(
            self.owner, "deadline.result", private=result, incomplete="error" in result
        )
        if ("error" in result or result.get("timed_out")) and not self.interruption.done():
            self.interruption.set_result("RESOURCE_DEADLINE")

    async def _wait(self, operation, *, seconds=None, interruptible=True):
        task = asyncio.ensure_future(operation)
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)
        task.add_done_callback(consume)
        watches = [task, self.failure]
        if interruptible:
            watches.append(self.interruption)
        await asyncio.wait(
            watches,
            timeout=seconds or self.limits.phase_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if self.failure.done():
            self.fenced = True  # Lost supervision cannot authorize further native work.
            error = self.failure.result()
            if isinstance(error, ProtocolError) and error.code in FENCED:
                raise error
            raise ProtocolError(
                "CONTROLLER_SERVICE_FAILED", "Controller supervision failed", "uncertain"
            ) from None
        if interruptible and self.interruption.done():
            raise ProtocolError(
                self.interruption.result(), "Trial execution was interrupted", "execution"
            )
        if task.done():
            return task.result()
        raise ProtocolError("PHASE_TIMEOUT", "Benchmark phase exceeded its deadline", "uncertain")

    async def _state(self):
        return await self._wait(
            self.store.control(self.owner), seconds=self.limits.service_seconds, interruptible=False
        )

    def _scoped(self, prepared=None, execution=None, capture=None):
        return {
            "scope": {
                "workspace_id": str(self.owner.workspace_id),
                "trial_id": str(self.owner.trial_id),
                "generation": self.owner.generation,
            },
            "payload": {
                "session_handle": None if prepared is None else prepared["session_handle"],
                "prepared": prepared,
                "execution": execution,
                "capture": capture,
            },
        }

    async def _note(self, event, **values):
        return await self._wait(
            self.store.note(self.owner, event, **values),
            seconds=self.limits.service_seconds,
            interruptible=False,
        )

    async def _capture_result(self, result, *, stage="capture"):
        if not isinstance(result, dict) or not isinstance(result.get("artifacts"), list):
            raise ProtocolError(
                "INVALID_CAPTURE_RESULT",
                "Worker capture did not return artifact references",
                "recording",
            )
        for artifact in result["artifacts"]:
            validate("artifact", artifact)
        await self._wait(
            self.capture_validator(self.owner, result, stage=stage),
            seconds=self.limits.capture_seconds,
            interruptible=False,
        )
        return result

    async def _interrupt(self, reason):
        channel = self.host.channel
        if channel is not None and not channel.client.peer.closed:
            try:
                await self._wait(
                    channel.invoke(
                        "cancel",
                        {**self._scoped(), "payload": {"reason": reason}},
                        timeout=self.limits.stop_seconds,
                    ),
                    seconds=self.limits.stop_seconds,
                    interruptible=False,
                )
            except (ProtocolError, TimeoutError):
                if self.fenced:
                    raise
        if self.agent_control is not None:
            await self._wait(
                self.agent_control.cancel(), seconds=self.limits.stop_seconds, interruptible=False
            )
        if self.pending:
            await asyncio.wait(set(self.pending), timeout=self.limits.stop_seconds)
        drained = await cancel_tasks(set(self.pending), self.limits.stop_seconds)
        if channel is not None:
            drained = await channel.cancel_pending(grace=self.limits.stop_seconds) and drained
        await self._note("controller.stopping", code=reason, incomplete=not drained)

    async def _capture_fallback(self):
        result = await self._wait(
            self.recovery.capture_files(self.owner, self.capture_providers),
            seconds=self.limits.capture_seconds,
            interruptible=False,
        )
        await self._note(
            "capture.completed" if result["complete"] else "capture.failed",
            code="FILE_RECOVERY",
            private=result,
            incomplete=not result["complete"],
        )
        return result["complete"]

    async def _cleanup(self, *, recovered, prepared, execution, captured, can_capture=True):
        state = await self._state()
        if state.phase != "cleaning":
            await self._wait(
                self.store.transition(self.owner, state.phase, "cleaning"), interruptible=False
            )
        await self._wait(self.store.finish_evaluation(self.owner), interruptible=False)
        quiet = await self._wait(
            self.recovery.sweep(self.owner, "stop", active_only=not recovered),
            seconds=self.limits.cleanup_seconds,
            interruptible=False,
        )
        if any("error" in result for result in quiet):
            await self._note(
                "cleanup.failed", code="QUIESCENCE_PENDING", private=quiet, incomplete=True
            )
            return
        if not captured:
            channel = self.host.channel
            if (
                can_capture
                and not recovered
                and channel is not None
                and not channel.client.peer.closed
                and prepared is not None
            ):
                try:
                    capture = await self._wait(
                        channel.invoke(
                            "capture",
                            self._scoped(prepared, execution),
                            timeout=self.limits.capture_seconds,
                        ),
                        seconds=self.limits.capture_seconds,
                        interruptible=False,
                    )
                    await self._capture_result(capture)
                    await self._note("capture.completed", private=capture)
                    captured = True
                except (ProtocolError, TimeoutError):
                    if self.fenced:
                        raise
                    await channel.cancel_pending(grace=self.limits.stop_seconds)
            if not captured:
                captured = await self._capture_fallback()
        if not captured:
            await self._note("cleanup.failed", code="EVIDENCE_CAPTURE_PENDING")
            return  # Preserve the files and reservation for a capture retry.
        channel = self.host.channel
        if not recovered and channel is not None and not channel.client.peer.closed:
            try:
                await self._wait(
                    channel.invoke(
                        "cleanup",
                        self._scoped(prepared, execution),
                        timeout=self.limits.cleanup_seconds,
                    ),
                    seconds=self.limits.cleanup_seconds,
                    interruptible=False,
                )
            except (ProtocolError, TimeoutError):
                if self.fenced:
                    raise
                await self._note("cleanup.failed", code="NATIVE_CLEANUP_FAILED", cleanup_error=True)
            try:
                await self._wait(
                    channel.close(grace=self.limits.stop_seconds),
                    seconds=6 * self.limits.stop_seconds + self.limits.service_seconds,
                    interruptible=False,
                )
            except (ProtocolError, TimeoutError):
                if self.fenced:
                    raise
                await self._note(
                    "controller.failed", code="WORKER_CLOSE_INCOMPLETE", incomplete=True
                )
        released = await self._wait(
            self.recovery.sweep(self.owner, "release"),
            seconds=self.limits.cleanup_seconds,
            interruptible=False,
        )
        if any("error" in result for result in released):
            await self._note("cleanup.failed", code="RESOURCE_RELEASE_PENDING", private=released)
            return
        self.stopping.set()
        await bounded(
            self.store.transition(self.owner, "cleaning", "finished"), self.limits.service_seconds
        )

    async def run(self, spec, *, private_inputs=None, extra_demands=(), evaluate=True):
        if self.used:
            raise ValueError("A driver cannot restart or resume a native session")
        spec = deepcopy(spec)
        self.used, self.fenced = True, False
        self.stopping = asyncio.Event()
        self.failure, self.interruption = (
            asyncio.get_running_loop().create_future() for _ in range(2)
        )
        loops = [
            self._heartbeats(),
            self._control(),
            *[
                service.run(self.owner, self.stopping, self._deadline_result)
                for service in self.deadlines
            ],
        ]
        self.services = [asyncio.create_task(self._service(operation)) for operation in loops]
        prepared, execution, captured, recovered, caller_cancelled = None, None, False, False, False
        try:
            state = await self._state()
            recovered = state.phase == "cleaning"
            if not recovered:
                if state.phase != "created":
                    self.fenced = True
                    raise ProtocolError(
                        "NATIVE_RESUME_UNSUPPORTED",
                        "A partial native session requires a new recovery claim",
                        "uncertain",
                    )
                validate_spec(self.host.manifest, spec)
                if spec.get("purpose", "execution") != "execution":
                    raise ProtocolError(
                        "PURPOSE_CONFLICT",
                        "Execution driver requires an execution spec",
                        "configuration",
                    )
                if evaluate and self.host.manifest["capabilities"].get("evaluate") not in {
                    "native",
                    "emulated",
                }:
                    raise ProtocolError(
                        "UNSUPPORTED_EVALUATION", "Native evaluation is unavailable", "unsupported"
                    )
                if state.seconds_remaining > spec["limits"]["wall_time_seconds"] + 1:
                    raise ProtocolError(
                        "INVALID_DEADLINE",
                        "Stored trial deadline exceeds the requested wall budget",
                        "configuration",
                    )
                entered = await self._wait(self.store.transition(self.owner, "created", "planning"))
                if entered["duplicate"]:
                    self.fenced = True
                    raise ProtocolError(
                        "OWNER_ACTIVE", "Another driver already entered this epoch", "configuration"
                    )
                channel = await self._wait(self.host.start())
                checked = await self._wait(
                    channel.invoke("validate", {"spec": spec}, timeout=self.limits.phase_seconds)
                )
                spec = {**deepcopy(spec), "config": checked["effective_config"]}
                validate_spec(self.host.manifest, spec)
                await self._note("configuration.applied", private=spec)
                plan = await self._wait(
                    channel.invoke("plan", {"spec": spec}, timeout=self.limits.phase_seconds)
                )
                await self._wait(
                    self.host.install_plan(
                        spec, plan, private_inputs=private_inputs, extra_demands=extra_demands
                    )
                )
                await self._wait(self.store.transition(self.owner, "planning", "preparing"))
                prepared = await self._wait(
                    channel.invoke(
                        "prepare", {"spec": spec, "plan": plan}, timeout=self.limits.phase_seconds
                    )
                )
                await self._wait(self.store.transition(self.owner, "preparing", "ready"))
                await self._wait(self.store.transition(self.owner, "ready", "driving"))
                execution = await self._wait(
                    channel.invoke(
                        "drive", self._scoped(prepared), timeout=self.limits.phase_seconds
                    )
                )
                await self._wait(self.store.finish_execution(self.owner, "success"))
                await self._wait(self.store.transition(self.owner, "driving", "capturing"))
                capture = await self._wait(
                    channel.invoke(
                        "capture",
                        self._scoped(prepared, execution),
                        timeout=self.limits.capture_seconds,
                    ),
                    seconds=self.limits.capture_seconds,
                )
                await self._capture_result(capture)
                await self._note("capture.completed", private=capture)
                captured = True
                if evaluate:
                    evaluation_id = await self._wait(
                        self.store.begin_evaluation(
                            self.owner,
                            {
                                "spec": spec,
                                "capture": capture,
                                "prepared": prepared,
                                "execution": execution,
                                "worker_actor_id": str(channel.actor_id),
                                "evaluator": {
                                    key: self.host.manifest[key]
                                    for key in (
                                        "id",
                                        "version",
                                        "protocol_version",
                                        "image",
                                        "capabilities",
                                    )
                                },
                            },
                        )
                    )
                    params = self._scoped(prepared, execution, capture)
                    params["payload"]["evaluation_id"] = str(evaluation_id)
                    result = await self._wait(
                        channel.invoke(
                            "evaluate",
                            params,
                            request_id="evaluation." + str(evaluation_id),
                            timeout=self.limits.phase_seconds,
                        )
                    )
                    artifacts = list(capture["artifacts"])
                    for artifact in result["artifacts"]:
                        if artifact not in artifacts:
                            artifacts.append(artifact)
                    await self._capture_result(
                        {**capture, "artifacts": artifacts},
                        stage="evaluation" if result["status"] == "completed" else "capture",
                    )
                    await self._wait(
                        self.store.finish_evaluation(self.owner, result, attempt_id=evaluation_id)
                    )
                else:
                    await self._wait(self.store.finish_evaluation(self.owner))
        except asyncio.CancelledError:
            caller_cancelled = True
            if not self.fenced:
                try:
                    await bounded(
                        self.store.request_stop(self.owner),
                        self.limits.service_seconds,
                    )
                    await self._wait(
                        self.store.fail_evaluation(
                            self.owner, kind="execution", code="CANCELLED", cancelled=True
                        ),
                        interruptible=False,
                    )
                    await self._interrupt("CANCELLED")
                except Exception:
                    self.fenced = True
        except Exception as error:
            if isinstance(error, ProtocolError) and error.code in FENCED:
                self.fenced = True
            if not self.fenced:
                code = (
                    error.code
                    if isinstance(error, ProtocolError)
                    and error.code
                    in {
                        "CANCELLED",
                        "TRIAL_DEADLINE",
                        "RESOURCE_DEADLINE",
                        "PHASE_TIMEOUT",
                        "INVALID_DEADLINE",
                        "NATIVE_RESUME_UNSUPPORTED",
                    }
                    else "CONTROLLER_FAILED"
                )
                try:
                    state = await self._state()
                    if state.cancelled:
                        code = "CANCELLED"
                    elif state.seconds_remaining <= 0:
                        code = "TRIAL_DEADLINE"
                    await self._note(
                        "controller.failed",
                        code=code,
                        incomplete=not isinstance(error, ProtocolError)
                        or error.kind in {"uncertain", "transport", "recording"},
                    )
                    await self._wait(
                        self.store.fail_evaluation(
                            self.owner,
                            kind=error.kind if isinstance(error, ProtocolError) else "uncertain",
                            code=code,
                            cancelled=code == "CANCELLED",
                        ),
                        interruptible=False,
                    )
                    await self._interrupt(code)
                except Exception:
                    self.fenced = True
        try:
            if not self.fenced:
                await self._cleanup(
                    recovered=recovered, prepared=prepared, execution=execution, captured=captured
                )
        except Exception:
            # Fenced/unknown effects remain owned by durable recovery. Never mark
            # resources released or execution successful after failed teardown.
            self.fenced = True
        finally:
            self.stopping.set()
            await cancel_tasks(set(self.pending) | set(self.services), self.limits.stop_seconds)
            try:
                await bounded(
                    self.host.detach(grace=self.limits.stop_seconds), 4 * self.limits.stop_seconds
                )
            except Exception:
                pass
        if caller_cancelled:
            raise asyncio.CancelledError
        if self.fenced:
            return {
                "trial_id": str(self.owner.trial_id),
                "controller": "detached",
                "cleanup": "pending",
            }
        state = await bounded(self.store.control(self.owner), self.limits.service_seconds)
        return {
            "trial_id": str(self.owner.trial_id),
            "phase": state.phase,
            "execution": state.execution_status,
            "evaluation": state.evaluation_status,
            "recording": state.recording_status,
            "cleanup": state.cleanup_status,
        }
