"""SDK resource RPC over the neutral rootless runtime and mandatory host grants."""

from __future__ import annotations

import asyncio
import math
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from docker.errors import DockerException, NotFound
from evalhive_benchmark import EnvironmentProvider, ProtocolError, validate

from app.runtime.broker import ExecutionGrant, ResourceLease
from app.runtime.finalizers import finalizer_payload
from app.runtime.managed_attach import ManagedDockerProcess
from app.runtime.managed_exec import OwnedDockerProcess, OwnedExecRuntime
from app.runtime.managed_spec import (
    ContainerSpec,
    ExecSpec,
    ManagedContainer,
    ManagedExec,
    ManagedNetwork,
    ManagedRuntimeError,
    ManagedVolume,
    NetworkRequest,
    UncertainRuntimeError,
    VolumeRequest,
    absolute_path,
)
from app.runtime.managed_storage import RootlessVolumeRuntime

MAX_OUTPUT = 65536


def _shape(payload, required, optional=()):
    if set(payload) - set(required) - set(optional) or set(required) - set(payload):
        raise ProtocolError(
            "INVALID_RESOURCE_REQUEST", "Unsupported resource request fields", "configuration"
        )


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value):
        raise ProtocolError(
            "INVALID_RESOURCE_REQUEST", "Resource identity is invalid", "configuration"
        )
    return value


class RootlessDockerProvider(EnvironmentProvider):
    """The coordinator supplies durable authority, installed plans and artifact storage.

    Active transports below are local connections, never the source of resource
    ownership. A restarted process must reconcile stored native handles; it cannot
    silently reconstruct a lost SDK stdout stream or resume a native session.
    """

    def __init__(self, runtime, authority, manifest, *, artifacts=None):
        validate("manifest", manifest)
        if manifest["kind"] != "environment_provider" or authority is None:
            raise ManagedRuntimeError("Rootless provider requires a manifest and host authority")
        self.runtime, self.authority, self.manifest = runtime, authority, manifest
        self.owned = OwnedExecRuntime(runtime)
        self.storage = RootlessVolumeRuntime(runtime)
        self.artifacts = artifacts
        self._processes, self._attached = {}, set()
        self._safety_tasks = set()

    async def _control(self, scope, handle, operation):
        return await self.handle(
            {
                "protocol_version": "1.0",
                "type": "broker_request",
                "id": uuid.uuid4().hex,
                "scope": {
                    "workspace_id": scope.workspace_id,
                    "trial_id": scope.trial_id,
                    "generation": scope.generation,
                },
                "provider": self.manifest["id"],
                "operation": operation,
                "handle": handle,
                "payload": {},
            }
        )

    def _schedule_safety_stop(self, scope, handle):
        task = asyncio.create_task(self._control(scope, handle, "stop"))
        self._safety_tasks.add(task)
        task.add_done_callback(self._safety_tasks.discard)
        return task

    @staticmethod
    def _lease(lease: ResourceLease, scope):
        if lease.scope != scope or getattr(lease.intent, "scope", scope) != scope:
            raise ProtocolError(
                "FOREIGN_HANDLE", "Resource belongs to another owner generation", "configuration"
            )
        if isinstance(lease.intent, ExecSpec) and lease.intent.container.scope != scope:
            raise ProtocolError(
                "FOREIGN_HANDLE", "Execution belongs to another environment", "configuration"
            )
        expected = {
            "container": (ContainerSpec, ManagedContainer),
            "network": (NetworkRequest, ManagedNetwork),
            "volume": (VolumeRequest, ManagedVolume),
            "exec": (ExecSpec, ManagedExec),
        }
        if lease.kind not in expected or not isinstance(lease.intent, expected[lease.kind][0]):
            raise ManagedRuntimeError("Stored resource declaration is invalid")
        native = lease.native
        if native is not None:
            if not isinstance(native, expected[lease.kind][1]):
                raise ManagedRuntimeError("Stored native resource type is invalid")
            native_scope = (
                native.container.scope if isinstance(native, ManagedExec) else native.scope
            )
            if native_scope != scope or native.resource_id != lease.intent.resource_id:
                raise ProtocolError(
                    "FOREIGN_HANDLE", "Native resource does not match its lease", "configuration"
                )
            if isinstance(native, ManagedContainer) and native.image_id != lease.intent.image_id:
                raise ManagedRuntimeError("Native image does not match its resource intent")
            if isinstance(native, ManagedExec) and (
                native.container != lease.intent.container
                or native.control_key != lease.intent.control_key
                or native.argv != OwnedExecRuntime._argv(lease.intent)
            ):
                raise ManagedRuntimeError("Native execution does not match its resource intent")
        return lease

    @staticmethod
    def _scope(call, request):
        if request["scope"] != {
            "workspace_id": call.scope.workspace_id,
            "trial_id": call.scope.trial_id,
            "generation": call.scope.generation,
        }:
            raise ProtocolError(
                "STALE_GENERATION", "Resource request scope is not current", "configuration"
            )

    async def handle(self, request, context=None):
        validate("broker_request", request)
        if request["provider"] != self.manifest["id"]:
            raise ProtocolError(
                "PROVIDER_MISMATCH", "Resource request targets another provider", "configuration"
            )
        operation = request["operation"]
        if self.manifest["capabilities"].get(operation) != "native":
            raise ProtocolError(
                "UNSUPPORTED_CAPABILITY", "Resource operation is unavailable", "unsupported"
            )
        async with self.authority.operation(request) as call:
            self._scope(call, request)
            if call.replay is not None:
                return call.replay
            try:
                result = await self._dispatch(call, request)
                await call.complete(result)
                return result
            except UncertainRuntimeError:
                raise ProtocolError(
                    "RESOURCE_UNCERTAIN", "Resource outcome requires reconciliation", "uncertain"
                ) from None
            except ManagedRuntimeError:
                raise ProtocolError(
                    "RESOURCE_REJECTED", "Resource operation failed its host checks", "preparation"
                ) from None
            except DockerException:
                raise ProtocolError(
                    "RESOURCE_UNCERTAIN", "Resource transport requires reconciliation", "uncertain"
                ) from None

    async def _dispatch(self, call, request):
        operation, payload = request["operation"], request["payload"]
        if operation == "allocate":
            if "handle" in request:
                raise ProtocolError(
                    "INVALID_RESOURCE_REQUEST",
                    "Allocation takes a plan resource ID",
                    "configuration",
                )
            _shape(payload, {"resource_id"})
            lease = self._lease(
                await call.allocation(_identifier(payload["resource_id"])), call.scope
            )
            if lease.state == "allocated" and lease.native is not None:
                state = (await self._inspect(lease))["native"]
                if (
                    lease.kind == "container"
                    and state is not None
                    and state["status"] == "running"
                    or lease.kind == "network"
                    and state["present"]
                    or lease.kind == "volume"
                    and state["present"]
                ):
                    return {"handle": lease.handle, "kind": lease.kind, "state": "allocated"}
                raise ProtocolError(
                    "RESOURCE_NOT_READY", "Allocated resource is no longer ready", "preparation"
                )
            if lease.state != "planned" or lease.native is not None:
                raise ProtocolError(
                    "RESOURCE_UNCERTAIN", "Existing allocation must be reconciled", "uncertain"
                )
            if lease.kind not in {"container", "network", "volume"}:
                raise ProtocolError(
                    "INVALID_RESOURCE_REQUEST", "Execs require the exec operation", "configuration"
                )
            await call.save(replace(lease, state="creating"))
            try:
                if lease.kind == "container":
                    native = await asyncio.to_thread(self.runtime.create, lease.intent)
                elif lease.kind == "volume":
                    native = await asyncio.to_thread(self.storage.create, lease.intent)
                else:
                    spec = lease.intent
                    native = await asyncio.to_thread(
                        self.runtime.create_network,
                        spec.scope,
                        spec.resource_id,
                        egress=spec.egress,
                    )
            except UncertainRuntimeError:
                raise
            except ManagedRuntimeError:
                # Rootless create methods raise this only from read-only checks.
                # Daemon mutations and lost replies use UncertainRuntimeError.
                await call.save(replace(lease, state="rejected"))
                raise
            await call.save(
                self._lease(replace(lease, native=native, state="allocated"), call.scope)
            )
            return {"handle": lease.handle, "kind": lease.kind, "state": "allocated"}
        if "handle" not in request:
            raise ProtocolError(
                "FOREIGN_HANDLE", "An owned resource handle is required", "configuration"
            )
        lease = self._lease(await call.resource(_identifier(request["handle"])), call.scope)
        if lease.handle != request["handle"]:
            raise ProtocolError(
                "FOREIGN_HANDLE", "Resolved resource does not match the handle", "configuration"
            )
        if operation == "exec":
            return await self._exec(call, lease, payload)
        if operation == "export":
            return await self._export(call, lease, payload)
        _shape(payload, set())
        if operation == "inspect":
            return await self._inspect(lease)
        if operation in {"stop", "release"}:
            return await self._stop(call, lease, release=operation == "release")
        raise ProtocolError(
            "UNSUPPORTED_CAPABILITY", "Resource operation is unavailable", "unsupported"
        )

    async def replay(self, request):
        """Resolve an acknowledged IPC tombstone from the authority, never dispatch.

        This pure receipt path can create a rejected lookup record when no
        original receipt exists, but never invokes a native runtime operation.
        """
        validate("broker_request", request)
        if request["provider"] != self.manifest["id"]:
            raise ProtocolError(
                "PROVIDER_MISMATCH", "Receipt targets another provider", "configuration"
            )
        async with self.authority.operation(request) as call:
            self._scope(call, request)
            if call.replay is None:
                raise ProtocolError(
                    "RECEIPT_UNAVAILABLE", "A durable resource receipt is unavailable", "uncertain"
                )
            return call.replay

    async def _inspect(self, lease):
        if lease.native is None:
            return {"handle": lease.handle, "kind": lease.kind, "state": lease.state}
        if lease.kind == "container":
            native = await asyncio.to_thread(self.runtime.inspect, lease.native)
        elif lease.kind == "network":
            resource = await asyncio.to_thread(self.runtime.network, lease.native)
            native = {"present": resource is not None}
        elif lease.kind == "volume":
            native = {
                "present": await asyncio.to_thread(self.storage.get, lease.native) is not None
            }
        else:
            parent = await asyncio.to_thread(self.runtime.inspect, lease.native.container)
            if parent is None or parent["status"] in {"exited", "dead", "created"}:
                native = {"stopped": True, "container_stopped": True}
            else:
                native = await asyncio.to_thread(self.owned.control, lease.native, "status")
        # Do not publish raw Docker IDs, argv, environment or private native state.
        return {"handle": lease.handle, "kind": lease.kind, "state": lease.state, "native": native}

    async def _exec(self, call, container, payload, *, finalizer=False):
        _shape(
            payload,
            {"argv"},
            {
                "cwd",
                "user",
                "environment",
                "private_input_refs",
                "mode",
                "timeout_seconds",
                "deadline_policy",
                "process_policy",
            },
        )
        process_policy = payload.get("process_policy", "retain_until_cleanup")
        if (
            not isinstance(process_policy, str)
            or process_policy not in {"retain_until_cleanup", "stop_after_command"}
            or (process_policy == "stop_after_command" and payload.get("mode", "wait") != "wait")
        ):
            raise ProtocolError(
                "INVALID_RESOURCE_REQUEST",
                "Native process policy requires a supported wait mode",
                "configuration",
            )
        if (
            container.kind != "container"
            or container.native is None
            or container.state
            not in ({"allocated", "stopping", "releasing"} if finalizer else {"allocated"})
        ):
            raise ProtocolError(
                "RESOURCE_NOT_READY", "Exec requires an allocated task container", "preparation"
            )
        if (
            not isinstance(payload["argv"], list)
            or not payload["argv"]
            or len(payload["argv"]) > 128
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in payload["argv"])
        ):
            raise ProtocolError(
                "INVALID_RESOURCE_REQUEST", "Native command argv is invalid", "configuration"
            )
        if "cwd" in payload:
            absolute_path(payload["cwd"])
        environment, references = (
            payload.get("environment", {}),
            payload.get("private_input_refs", {}),
        )
        if (
            not isinstance(environment, dict)
            or not isinstance(references, dict)
            or len(environment) > 256
            or len(references) > 16
            or any(
                not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,126}", name)
                or not isinstance(value, str)
                or "\0" in value
                for name, value in environment.items()
            )
            or any(
                not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,126}", name)
                or not isinstance(value, str)
                or not value
                or len(value) > 200
                for name, value in references.items()
            )
            or set(environment) & set(references)
            or payload.get("mode", "wait") not in {"wait", "attach"}
            or not isinstance(payload.get("deadline_policy", "strict"), str)
            or payload.get("deadline_policy", "strict") not in {"strict", "inherit"}
            or (
                "user" in payload
                and (
                    not isinstance(payload["user"], str)
                    or not payload["user"]
                    or "\0" in payload["user"]
                )
            )
        ):
            raise ProtocolError(
                "INVALID_RESOURCE_REQUEST", "Native command settings are invalid", "configuration"
            )
        if "timeout_seconds" in payload:
            timeout = payload["timeout_seconds"]
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout)
                or not 0 < timeout <= 86400
            ):
                raise ProtocolError(
                    "INVALID_RESOURCE_REQUEST",
                    "Native command deadline is invalid",
                    "configuration",
                )
        grant: ExecutionGrant = await (
            call.finalizer_execution(container, payload)
            if finalizer
            else call.execution(container, payload)
        )
        lease = self._lease(grant.lease, call.scope)
        if (
            lease.kind != "exec"
            or lease.state != "planned"
            or lease.native is not None
            or lease.intent.container != container.native
            or grant.mode not in {"wait", "attach"}
            or isinstance(grant.timeout_seconds, bool)
            or not isinstance(grant.timeout_seconds, (int, float))
            or not math.isfinite(grant.timeout_seconds)
            or not 0 < grant.timeout_seconds <= 86400
        ):
            raise ManagedRuntimeError("Host execution grant is invalid")
        if (
            lease.intent.argv != tuple(payload["argv"])
            or grant.mode != payload.get("mode", "wait")
            or lease.intent.workdir != payload.get("cwd")
            or lease.intent.user != payload.get("user")
            or lease.intent.environment != payload.get("environment", {})
            or set(lease.intent.private_inputs) != set(payload.get("private_input_refs", {}))
            or (
                "timeout_seconds" in payload
                and (
                    grant.timeout_seconds > payload["timeout_seconds"]
                    or (
                        payload.get("deadline_policy", "strict") != "inherit"
                        and payload["timeout_seconds"] != grant.timeout_seconds
                    )
                )
            )
        ):
            raise ManagedRuntimeError("Execution grant changed the requested native settings")
        native, process = None, None
        lease = replace(
            lease,
            deadline_at=(datetime.now(UTC) + timedelta(seconds=grant.timeout_seconds)).isoformat(),
        )
        output, truncated = bytearray(), False
        try:
            async with asyncio.timeout(grant.timeout_seconds):
                await call.save(replace(lease, state="creating"))
                try:
                    native = await asyncio.to_thread(self.owned.create, lease.intent)
                except UncertainRuntimeError:
                    raise
                except ManagedRuntimeError:
                    await call.save(replace(lease, state="rejected"))
                    raise
                lease = self._lease(replace(lease, native=native, state="created"), call.scope)
                await call.save(lease)  # Persist control identity before starting it.
                process = await OwnedDockerProcess.start(
                    self.owned,
                    native,
                    lease.intent,
                    control=lambda op: self._control(call.scope, lease.handle, op),
                    on_broken=lambda: self._schedule_safety_stop(call.scope, container.handle),
                )
                self._processes[lease.handle] = process
                lease = replace(lease, state="running")
                await call.save(lease)
                if grant.mode == "attach":
                    return {
                        "handle": lease.handle,
                        "kind": "exec",
                        "state": "running",
                        "deadline_at": lease.deadline_at,
                        **(
                            {"effective_timeout_seconds": grant.timeout_seconds}
                            if payload.get("deadline_policy") == "inherit"
                            else {}
                        ),
                    }
                async with call.observation():
                    while chunk := await process.stdout.read():
                        remaining = max(0, MAX_OUTPUT - len(output))
                        output.extend(chunk[:remaining])
                        truncated |= len(chunk) > remaining
                    code = await process.wait()
        except ManagedRuntimeError:
            if process is not None and process.environment_broken:
                parent = await call.resource(container.handle)
                if parent.state not in {"stopped", "released"}:
                    await call.save(replace(parent, state="unsafe"))
                raise ProtocolError(
                    "ENVIRONMENT_UNSAFE",
                    "Native execution lost safe environment ownership",
                    "recording",
                ) from None
            raise
        except (TimeoutError, asyncio.CancelledError) as error:
            if native is None:
                raise UncertainRuntimeError(
                    "Native command startup requires reconciliation"
                ) from None
            try:
                current = await call.resource(lease.handle)
                if current.state not in {"retained", "stopped", "released"}:
                    state = await asyncio.wait_for(
                        asyncio.shield(asyncio.to_thread(self.owned.control, native, "expire")), 9
                    )
                    await call.save(
                        replace(current, state="stopped" if state["stopped"] else "retained")
                    )
            except Exception:
                raise UncertainRuntimeError(
                    "Native command cancellation requires reconciliation"
                ) from None
            if isinstance(error, asyncio.CancelledError):
                raise
            raise ProtocolError(
                "RESOURCE_TIMEOUT", "Native command exceeded its deadline", "execution"
            ) from None
        lease = await call.resource(lease.handle)
        if process_policy == "stop_after_command":
            await call.save(replace(lease, state="releasing"))
            try:
                await asyncio.wait_for(asyncio.to_thread(self.owned.control, native, "release"), 9)
            except TimeoutError:
                raise UncertainRuntimeError(
                    "Native command release requires reconciliation"
                ) from None
            await process.aclose()
            self._processes.pop(lease.handle, None)
            lease = replace(lease, state="released")
            await call.save(lease)
        elif lease.state not in {"stopped", "released", "unsafe"}:
            lease = replace(lease, state="retained")
            await call.save(lease)
        return {
            "handle": lease.handle,
            "kind": "exec",
            "state": lease.state,
            "exit_code": code,
            "stdout": output.decode("utf-8", errors="replace"),
            "stderr": bytes(process.stderr).decode("utf-8", errors="replace"),
            "stdout_truncated": truncated,
            "stderr_truncated": process.stderr_truncated,
            **(
                {"effective_timeout_seconds": grant.timeout_seconds}
                if payload.get("deadline_policy") == "inherit"
                else {}
            ),
        }

    async def finalize_resource(self, call, lease):
        if (
            not isinstance(lease.intent, ContainerSpec)
            or lease.intent.finalizer is None
            or lease.finalized
            or lease.native is None
        ):
            return lease
        state = await asyncio.to_thread(self.runtime.inspect, lease.native)
        if state is None or state["status"] != "running" or lease.state == "unsafe":
            raise ProtocolError(
                "RESOURCE_FINALIZATION_UNAVAILABLE",
                "Required cleanup cannot be confirmed in the owned resource",
                "cleanup",
            )
        try:
            result = await self._exec(call, lease, finalizer_payload(lease), finalizer=True)
            if result["exit_code"] != 0:
                raise ProtocolError(
                    "RESOURCE_FINALIZATION_FAILED",
                    "Required resource cleanup was not confirmed",
                    "cleanup",
                )
            lease = replace(lease, finalized=True)
            await call.save(lease)
            return lease
        except BaseException as error:
            if isinstance(error, ProtocolError) and error.code in {
                "STALE_GENERATION",
                "OWNER_EXPIRED",
                "TRIAL_NOT_FOUND",
                "TRIAL_FINISHED",
                "ACTOR_FORBIDDEN",
                "FOREIGN_HANDLE",
                "FINALIZER_FORBIDDEN",
            }:
                raise

            # A timed-out callback must not leave the original service mutating
            # external state. Keep its filesystem and unconfirmed cleanup proof.
            async def contain():
                # Reacquire the current SQL ownership fence before containment;
                # an expired controller cannot stop a successor's resource.
                async with call.observation():
                    pass
                await asyncio.to_thread(self.runtime.stop, lease.native, remove=False)
                await call.save(replace(lease, state="unsafe"))

            await asyncio.shield(contain())
            raise

    async def _stop(self, call, lease, *, release):
        if lease.state == "released":
            return {"handle": lease.handle, "released": True}
        if lease.kind in {"network", "volume"} and not release:
            raise ProtocolError(
                "UNSUPPORTED_CAPABILITY", "This resource supports release, not stop", "unsupported"
            )
        if lease.native is None:
            if lease.state not in {"planned", "rejected", "unallocated"}:
                raise UncertainRuntimeError("Unresolved resource intent cannot be released blindly")
        elif lease.kind == "container":
            lease = await self.finalize_resource(call, lease)
            await asyncio.to_thread(self.runtime.stop, lease.native, remove=release)
        elif lease.kind == "network":
            await asyncio.to_thread(self.runtime.remove_network, lease.native)
        elif lease.kind == "volume":
            await asyncio.to_thread(self.storage.remove, lease.native)
        else:
            parent = await asyncio.to_thread(self.runtime.inspect, lease.native.container)
            if parent is not None and parent["status"] not in {"exited", "dead", "created"}:
                await asyncio.to_thread(
                    self.owned.control, lease.native, "release" if release else "stop"
                )
        if release and (process := self._processes.pop(lease.handle, None)) is not None:
            await process.aclose()
            self._attached.discard(lease.handle)
        state = "released" if release else "unallocated" if lease.native is None else "stopped"
        await call.save(replace(lease, state=state))
        return {"handle": lease.handle, "released" if release else "stopped": True}

    async def _export(self, call, lease, payload):
        _shape(payload, {"output_id"})
        if self.artifacts is None:
            raise ProtocolError(
                "UNSUPPORTED_CAPABILITY", "Artifact storage is unavailable", "unsupported"
            )
        if lease.kind != "container" or lease.native is None:
            raise ProtocolError(
                "INVALID_RESOURCE_REQUEST", "Export requires a task container", "configuration"
            )
        grant = await call.output(lease, _identifier(payload["output_id"]))
        path = absolute_path(grant.path)
        if (
            grant.visibility not in {"agent", "user", "evaluator", "private"}
            or type(grant.max_archive_bytes) is not int
            or not 1 <= grant.max_archive_bytes <= 64 * 1024**3
            or type(grant.rootfs) is not bool
            or (grant.rootfs and grant.visibility != "private")
            or (len(path.parts) == 1 and grant.visibility != "private")
            or (grant.rootfs and grant.path != "/")
        ):
            raise ManagedRuntimeError("Host output grant is invalid")
        container = await asyncio.to_thread(self.runtime.container, lease.native)
        if container is None:
            raise ProtocolError(
                "RESOURCE_MISSING", "Environment evidence is unavailable", "recording"
            )
        try:
            if grant.rootfs:
                stream = await asyncio.to_thread(container.export)
            else:
                stream, _ = await asyncio.to_thread(container.get_archive, grant.path)
        except NotFound:
            raise ProtocolError(
                "ARTIFACT_MISSING", "Requested evidence does not exist", "recording"
            ) from None
        except DockerException:
            raise ProtocolError(
                "ARTIFACT_READ_FAILED", "Environment evidence could not be read", "recording"
            ) from None

        finished = False

        async def chunks():
            nonlocal finished
            total = 0
            iterator = iter(stream)
            sentinel = object()
            while True:
                value = await asyncio.to_thread(next, iterator, sentinel)
                if value is sentinel:
                    finished = True
                    break
                total += len(value)
                if total > grant.max_archive_bytes:
                    raise ProtocolError(
                        "ARTIFACT_TOO_LARGE", "Evidence exceeded its byte limit", "recording"
                    )
                yield value

        try:
            artifact = await self.artifacts.save(call.scope, grant, chunks(), publication=call)
            if not finished:
                raise ProtocolError(
                    "ARTIFACT_INCOMPLETE",
                    "Evidence storage did not consume the complete stream",
                    "recording",
                )
            validate("artifact", artifact)
            if artifact["id"] != grant.artifact_id or artifact["visibility"] != grant.visibility:
                raise ManagedRuntimeError("Stored artifact does not match its output grant")
            return {"artifact": artifact}
        finally:
            if hasattr(stream, "close"):
                await asyncio.to_thread(stream.close)

    async def open_transport(self, request):
        """Internal host API only. SDK RPC returns handles, never sockets/processes."""
        validate("broker_request", request)
        if request["operation"] != "inspect" or request["provider"] != self.manifest["id"]:
            raise ManagedRuntimeError("Transport access requires an owned inspect operation")
        async with self.authority.operation(request) as call:
            self._scope(call, request)
            if call.replay is not None:
                raise ManagedRuntimeError("Transport attachment cannot replay a lost connection")
            if "handle" not in request:
                raise ManagedRuntimeError("Transport access requires an exec handle")
            lease = self._lease(await call.resource(request["handle"]), call.scope)
            if lease.handle != request["handle"]:
                raise ManagedRuntimeError("Transport resource does not match its handle")
            process = self._processes.get(lease.handle)
            if lease.kind == "container":
                if (
                    lease.state != "allocated"
                    or lease.native is None
                    or process is not None
                    or lease.intent.role not in {"worker", "grader"}
                ):
                    raise ManagedRuntimeError("Worker transport is lost or already attached")
                process = await ManagedDockerProcess.attach(
                    self.runtime,
                    lease.native,
                    control=lambda op: self._control(call.scope, lease.handle, op),
                )
                self._processes[lease.handle] = process
            elif (
                lease.kind != "exec"
                or lease.state != "running"
                or process is None
                or process.handle != lease.native
            ):
                raise ManagedRuntimeError("Owned transport is lost or already attached")
            if lease.handle in self._attached:
                raise ManagedRuntimeError("Owned transport is lost or already attached")
            if lease.kind == "exec":
                await asyncio.to_thread(self.owned.inspect, lease.native)
            await call.complete({"handle": lease.handle, "transport_attached": True})
            self._attached.add(lease.handle)
            return process

    async def close(self, context=None):
        # Close local connections; the coordinator retains and reconciles leases.
        for handle, process in list(self._processes.items()):
            try:
                process.terminate()
                await asyncio.wait_for(asyncio.gather(*process._stop_tasks), 9)
            except Exception:
                pass  # Durable lease cleanup remains pending, never marked released.
            finally:
                await process.aclose()
                self._processes.pop(handle, None)
                self._attached.discard(handle)
        if self._safety_tasks:
            await asyncio.gather(*self._safety_tasks, return_exceptions=True)

    async def detach(self):
        """Abandon local transports; durable recovery alone owns native cleanup."""
        from contextlib import suppress

        from app.benchmarks.async_control import bounded, cancel_tasks

        await cancel_tasks(list(self._safety_tasks), 1)
        for process in list(self._processes.values()):
            with suppress(TimeoutError):
                await bounded(process.aclose(), 1)
        self._processes.clear()
        self._attached.clear()
