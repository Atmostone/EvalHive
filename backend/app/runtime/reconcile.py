"""Fenced observation and recovery of rootless intents; never repeat startup.

This is a host API, not an additional worker capability. The coordinator binds
the current controller and the resource's immutable origin through its authority.
Inspect/stop/release retain their actual permission checks and durable receipts.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from docker.errors import DockerException
from evalhive_benchmark import ProtocolError, validate

from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.managed_exec import OwnedExecRuntime
from app.runtime.managed_spec import ManagedRuntimeError, UncertainRuntimeError

STOPPED = frozenset({"created", "exited", "dead"})


class RootlessReconciler:
    def __init__(self, runtime, authority, provider_id):
        self.runtime, self.authority, self.provider_id = runtime, authority, provider_id
        self.owned = OwnedExecRuntime(runtime)

    async def handle(self, request):
        validate("broker_request", request)
        expire = request["operation"] == "stop" and request["payload"] == {"expire": True}
        if (
            request["provider"] != self.provider_id
            or "handle" not in request
            or (not expire and request["payload"] != {"reconcile": True})
            or request["operation"] not in {"inspect", "stop", "release"}
        ):
            raise ProtocolError(
                "INVALID_RECOVERY", "Invalid host reconciliation request", "configuration"
            )
        async with self.authority.operation(request) as call:
            RootlessDockerProvider._scope(call, request)
            if call.replay is not None:
                return call.replay
            lease = RootlessDockerProvider._lease(
                await call.resource(request["handle"]), call.scope
            )
            if lease.handle != request["handle"]:
                raise ProtocolError(
                    "FOREIGN_HANDLE", "Resolved resource differs from its request", "configuration"
                )
            try:
                if expire:
                    if lease.kind != "exec":
                        raise ProtocolError(
                            "INVALID_RECOVERY",
                            "Deadlines apply to an owned execution",
                            "configuration",
                        )
                    expired = await call.deadline_expired(lease)
                    result = (
                        await self._reconcile(call, lease, "expire")
                        if expired
                        else self._result(lease)
                    )
                    result["deadline_expired"] = expired
                else:
                    result = await self._reconcile(call, lease, request["operation"])
            except UncertainRuntimeError:
                raise ProtocolError(
                    "RESOURCE_UNCERTAIN",
                    "Native resource requires another reconciliation",
                    "uncertain",
                ) from None
            except ManagedRuntimeError:
                raise ProtocolError(
                    "RECOVERY_REJECTED", "Native resource failed its ownership checks", "cleanup"
                ) from None
            except DockerException:
                raise ProtocolError(
                    "RESOURCE_UNCERTAIN", "Native resource inspection is unavailable", "uncertain"
                ) from None
            await call.complete(result)
            return result

    async def _save(self, call, lease, **changes):
        updated = RootlessDockerProvider._lease(replace(lease, **changes), call.scope)
        if updated != lease:
            await call.save(updated)
        return updated

    async def _reconcile(self, call, lease, operation):
        if lease.state == "released":
            return self._result(lease)
        if lease.kind == "exec":
            return await self._exec(call, lease, operation)
        if lease.native is None:
            if lease.state == "planned":
                if operation == "release":
                    lease = await self._save(call, lease, state="released")
                return self._result(lease)
            if lease.kind == "container":
                native = await asyncio.to_thread(
                    self.runtime.find, lease.scope, lease.intent.resource_id
                )
            else:
                native = await asyncio.to_thread(
                    self.runtime.find_network, lease.scope, lease.intent.resource_id
                )
            if native is None:
                # An outstanding daemon create may finish after this observation.
                # Absence on one scan cannot release an unconfirmed creation.
                raise UncertainRuntimeError("Unconfirmed creation has no native receipt")
            lease = await self._save(call, lease, native=native, state="allocated")
        if lease.kind == "network":
            network = await asyncio.to_thread(self.runtime.network, lease.native)
            if network is None:
                lease = await self._save(call, lease, state="released")
                return self._result(lease, missing=True)
            if operation == "release":
                lease = await self._save(call, lease, state="releasing")
                await asyncio.to_thread(self.runtime.remove_network, lease.native)
                if await asyncio.to_thread(self.runtime.network, lease.native) is not None:
                    raise UncertainRuntimeError("Network removal has not completed")
                lease = await self._save(call, lease, state="released")
            return self._result(lease)
        state = await asyncio.to_thread(self.runtime.inspect, lease.native)
        if state is None:
            lease = await self._save(call, lease, state="released")
            return self._result(lease, missing=True)
        if operation in {"stop", "release"}:
            lease = await self._save(
                call, lease, state="releasing" if operation == "release" else "stopping"
            )
            await asyncio.to_thread(self.runtime.stop, lease.native, remove=operation == "release")
            state = await asyncio.to_thread(self.runtime.inspect, lease.native)
            if state is None:
                lease = await self._save(call, lease, state="released")
            elif operation == "release" or state["status"] not in STOPPED:
                raise UncertainRuntimeError("Environment quiescence has not been confirmed")
            else:
                lease = await self._save(call, lease, state="stopped")
        elif state["status"] in STOPPED and lease.state not in {"stopped", "unsafe"}:
            lease = await self._save(call, lease, state="stopped")
        return self._result(lease)

    async def _exec(self, call, lease, operation):
        parent = RootlessDockerProvider._lease(
            await call.resource(lease.intent.container.resource_id), call.scope
        )
        if parent.kind != "container" or parent.native != lease.intent.container:
            raise ManagedRuntimeError("Execution parent does not match its environment lease")
        state = await asyncio.to_thread(self.runtime.inspect, parent.native)
        if state is None or state["status"] in STOPPED:
            # exec_start cannot restart its stopped parent. This also covers a
            # created-but-never-started or missing Docker exec identity.
            lease = await self._save(
                call, lease, state="released" if operation == "release" else "stopped"
            )
            return self._result(lease, container_stopped=True)
        if state["status"] != "running":
            raise UncertainRuntimeError("Execution parent is not in a stable native state")
        try:
            if lease.native is None:
                native = await asyncio.to_thread(self.owned.find, lease.intent)
                if native is None:
                    raise ManagedRuntimeError("Owned supervisor is unavailable")
                lease = await self._save(call, lease, native=native, state="created")
            status = await asyncio.to_thread(self.owned.control, lease.native, "status")
        except ManagedRuntimeError:
            # A broken supervisor cannot prove that its detached descendants are
            # gone. Mark the environment unsafe before any attempt to quiesce it.
            await self._save(call, parent, state="unsafe")
            lease = await self._save(call, lease, state="unsafe")
            if operation == "inspect":
                return self._result(lease, environment_unsafe=True)
            await self._save(call, parent, state="stopping")
            await asyncio.to_thread(self.runtime.stop, parent.native, remove=False)
            state = await asyncio.to_thread(self.runtime.inspect, parent.native)
            if state is not None and state["status"] not in STOPPED:
                raise UncertainRuntimeError("Unsafe environment could not be quiesced") from None
            await self._save(call, parent, state="unsafe" if state else "released")
            lease = await self._save(
                call, lease, state="released" if operation == "release" else "stopped"
            )
            return self._result(lease, environment_unsafe=True, container_stopped=True)
        if operation in {"stop", "release"}:
            lease = await self._save(
                call, lease, state="releasing" if operation == "release" else "stopping"
            )
            await asyncio.to_thread(self.owned.control, lease.native, operation)
            lease = await self._save(
                call, lease, state="released" if operation == "release" else "stopped"
            )
        elif operation == "expire":
            lease = await self._save(call, lease, state="expiring")
            status = await asyncio.to_thread(self.owned.control, lease.native, "expire")
            lease = await self._save(
                call, lease, state="stopped" if status["stopped"] else "retained"
            )
            return self._result(
                lease,
                main_done=status["main_done"],
                exit_code=status["exit_code"],
                timed_out=status["stopped"],
            )
        elif status["stopped"]:
            lease = await self._save(call, lease, state="stopped")
        elif status["main_done"]:
            # Native completion retains background services for independent grading.
            lease = await self._save(call, lease, state="retained")
        else:
            lease = await self._save(call, lease, state="running")
        return self._result(lease, main_done=status["main_done"], exit_code=status["exit_code"])

    @staticmethod
    def _result(lease, **details):
        return {"handle": lease.handle, "kind": lease.kind, "state": lease.state, **details}
