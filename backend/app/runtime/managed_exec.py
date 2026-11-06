"""Owned native execs keep the task rootfs alive for independent grading.

The supervisor owns descendants, not the container. Its private control key is
never an argv/environment value and must be stored with the host's resource lease.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import struct
from contextlib import suppress

from docker.errors import DockerException

from app.runtime.managed_docker import _resource_id
from app.runtime.managed_spec import (
    ExecSpec,
    ManagedExec,
    ManagedRuntimeError,
    UncertainRuntimeError,
    absolute_path,
)

SUPERVISOR = "/evalhive-runtime/supervisor"
MAX_RECORD = 1024 * 1024


def _socket_path(resource_id):
    return "/tmp/evalhive-exec-" + _resource_id(resource_id) + ".sock"


def _raw(connection):
    return getattr(connection, "_sock", connection)


def _exact(stream, count):
    result = bytearray()
    while len(result) < count:
        value = stream.recv(count - len(result))
        if not value:
            raise ManagedRuntimeError("Owned exec control stream ended prematurely")
        result.extend(value)
    return bytes(result)


class OwnedExecRuntime:
    def __init__(self, runtime):
        self.runtime, self.api = runtime, runtime.client.api

    def _container(self, handle):
        container = self.runtime.container(handle)
        if container is None or container.status != "running":
            raise ManagedRuntimeError("Owned exec requires a running environment")
        # The root-level read-only mount cannot be renamed by a task process.
        if not any(
            mount.get("Destination") == "/evalhive-runtime"
            and mount.get("Type") == "bind"
            and mount.get("RW") is False
            for mount in container.attrs.get("Mounts", [])
        ):
            raise ManagedRuntimeError("Owned exec requires the registered read-only runtime bundle")
        return container

    @staticmethod
    def _argv(spec):
        _resource_id(spec.resource_id)
        if not spec.argv or any(
            not isinstance(value, str) or not value or "\0" in value for value in spec.argv
        ):
            raise ManagedRuntimeError("Owned exec requires a nonempty process argv")
        if type(spec.control_key) is not bytes or len(spec.control_key) != 32:
            raise ManagedRuntimeError("Owned exec requires a private control key")
        if spec.workdir is not None:
            absolute_path(spec.workdir)
        if spec.user is not None and (
            not isinstance(spec.user, str) or not spec.user or "\0" in spec.user
        ):
            raise ManagedRuntimeError("Invalid owned exec user")
        if any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,126}", key)
            or not isinstance(value, str)
            or "\0" in value
            for key, value in spec.environment.items()
        ):
            raise ManagedRuntimeError("Invalid owned exec environment")
        if len(spec.private_inputs) > 16 or any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,126}", key)
            or key in spec.environment
            or type(value) is not bytes
            or len(value) > MAX_RECORD
            for key, value in spec.private_inputs.items()
        ):
            raise ManagedRuntimeError("Invalid owned exec private inputs")
        argv = [SUPERVISOR, "--socket", _socket_path(spec.resource_id)]
        for name in sorted(spec.private_inputs):
            argv += ["--private-env", name]
        return tuple(argv + ["--", *spec.argv])

    def create(self, spec: ExecSpec) -> ManagedExec:
        # Keep validation outside the mutation boundary: a known rejection owns
        # no new exec, while a lost exec_create reply requires reconciliation.
        argv = self._argv(spec)
        self._container(spec.container)
        try:
            identity = self.api.exec_create(
                spec.container.container_id,
                cmd=list(argv),
                stdin=True,
                stdout=True,
                stderr=True,
                tty=False,
                workdir=spec.workdir,
                user=spec.user,
                environment=dict(spec.environment),
            )["Id"]
        except DockerException:
            raise UncertainRuntimeError(
                "Owned exec creation needs reconciliation before retry"
            ) from None
        return ManagedExec(
            spec.container,
            _resource_id(spec.resource_id),
            identity,
            argv,
            spec.control_key,
            spec.user,
        )

    def inspect(self, handle: ManagedExec):
        self._container(handle.container)
        try:
            state = self.api.exec_inspect(handle.exec_id)
        except DockerException:
            raise ManagedRuntimeError("Owned exec identity is unavailable") from None
        config = state.get("ProcessConfig", {})
        # Docker exec IDs have no labels: bind the ID to its container and to the
        # supervisor's deterministic resource socket before any control operation.
        if (
            state.get("ContainerID") != handle.container.container_id
            or tuple([config.get("entrypoint"), *config.get("arguments", [])]) != handle.argv
            or handle.argv[:3] != (SUPERVISOR, "--socket", _socket_path(handle.resource_id))
            or (handle.user is not None and config.get("user") != handle.user)
        ):
            raise ManagedRuntimeError("Exec does not belong to this resource generation")
        return {"running": state.get("Running"), "wrapper_exit_code": state.get("ExitCode")}

    def find(self, spec: ExecSpec) -> ManagedExec | None:
        argv = self._argv(spec)
        container = self._container(spec.container)
        matches = []
        try:
            for identity in container.attrs.get("ExecIDs") or []:
                state = self.api.exec_inspect(identity)
                config = state.get("ProcessConfig", {})
                if tuple([config.get("entrypoint"), *config.get("arguments", [])]) == argv:
                    candidate = ManagedExec(
                        spec.container,
                        _resource_id(spec.resource_id),
                        identity,
                        argv,
                        spec.control_key,
                        spec.user,
                    )
                    self.inspect(candidate)
                    matches.append(candidate)
        except DockerException:
            raise ManagedRuntimeError("Owned exec reconciliation is unavailable") from None
        if len(matches) > 1:
            raise ManagedRuntimeError("Resource identity has conflicting execs")
        return matches[0] if matches else None

    def start(self, handle: ManagedExec, spec: ExecSpec):
        if (
            self._argv(spec) != handle.argv
            or spec.container != handle.container
            or spec.control_key != handle.control_key
        ):
            raise ManagedRuntimeError("Owned exec start does not match its prepared intent")
        state = self.inspect(handle)
        if state["running"]:
            raise ManagedRuntimeError("Owned exec cannot be started twice")
        # Docker's inspect serializes both an unstarted exec and a successful
        # finished exec with ExitCode=0. The durable host intent gates startup;
        # the daemon atomically rejects an exec that has already run.
        connection = None
        try:
            connection = self.api.exec_start(handle.exec_id, socket=True)
            stream = _raw(connection)
            stream.settimeout(15)
            stream.sendall(handle.control_key)
            for name in sorted(spec.private_inputs):
                value = spec.private_inputs[name]
                stream.sendall(struct.pack(">I", len(value)))
                stream.sendall(value)
            return connection
        except (DockerException, OSError):
            if connection is not None:
                connection.close()
            raise UncertainRuntimeError(
                "Owned exec startup needs reconciliation before retry"
            ) from None

    def control(self, handle: ManagedExec, operation: str):
        if operation not in {"status", "stop", "release", "expire"}:
            raise ManagedRuntimeError("Unknown owned exec control operation")
        if not self.inspect(handle)["running"]:
            raise ManagedRuntimeError("Owned exec supervisor is no longer available")
        connection = None
        try:
            identity = self.api.exec_create(
                handle.container.container_id,
                cmd=[SUPERVISOR, "--" + operation, _socket_path(handle.resource_id)],
                user=handle.user,
                stdin=True,
                stdout=True,
                stderr=True,
                tty=False,
            )["Id"]
            connection = self.api.exec_start(identity, socket=True)
            stream = _raw(connection)
            stream.settimeout(10)
            stream.sendall(handle.control_key)
            stream.shutdown(socket.SHUT_WR)
            header = _exact(stream, 8)
            size = int.from_bytes(header[4:], "big")
            if header[:4] != b"\x01\0\0\0" or not 1 <= size <= 256:
                raise ValueError
            result = json.loads(_exact(stream, size))
            if (
                set(result) != {"main_done", "exit_code", "children", "stopped", "ok"}
                or any(type(result[key]) is not bool for key in ("main_done", "stopped", "ok"))
                or type(result["exit_code"]) is not int
                or not -1 <= result["exit_code"] <= 255
                or type(result["children"]) is not int
                or not 0 <= result["children"] <= 8192
                or not result["ok"]
                or (
                    operation in {"stop", "release"}
                    and (not result["stopped"] or result["children"] != 0)
                )
                or (
                    operation == "expire"
                    and (not result["main_done"] or (result["stopped"] and result["children"] != 0))
                )
            ):
                raise ValueError
            return result
        except (DockerException, OSError, ValueError, TypeError):
            raise UncertainRuntimeError("Owned exec control outcome needs reconciliation") from None
        finally:
            if connection is not None:
                connection.close()


class SupervisorOutput:
    """Strip authenticated lifecycle records without delaying complete JSONL lines."""

    def __init__(self, key):
        self.prefix = b"\x1eEVALHIVE_EXEC_V1 " + key.hex().encode() + b" "
        self.buffer = bytearray()
        self.ready = False
        self.exit_code = None
        self.children = None

    def feed(self, data):
        self.buffer.extend(data)
        output = bytearray()
        while self.buffer:
            index = self.buffer.find(self.prefix)
            if index < 0:
                keep = min(len(self.buffer), len(self.prefix) - 1)
                while keep and self.buffer[-keep:] != self.prefix[:keep]:
                    keep -= 1
                count = len(self.buffer) - keep
                output.extend(self.buffer[:count])
                del self.buffer[:count]
                break
            output.extend(self.buffer[:index])
            del self.buffer[:index]
            end = self.buffer.find(b"\n", len(self.prefix))
            if end < 0:
                if len(self.buffer) > 256:
                    raise ManagedRuntimeError("Invalid owned exec lifecycle record")
                break
            record = bytes(self.buffer[len(self.prefix) : end])
            del self.buffer[: end + 1]
            match = re.fullmatch(rb"(READY|EXIT) ([0-9]+) ([0-9]+)", record)
            if not match:
                raise ManagedRuntimeError("Invalid owned exec lifecycle record")
            kind, code, children = match.groups()
            code, children = int(code), int(children)
            if (
                kind == b"READY"
                and not self.ready
                and self.exit_code is None
                and code > 0
                and children == 0
            ):
                self.ready = True
            elif (
                kind == b"EXIT"
                and self.ready
                and self.exit_code is None
                and code <= 255
                and children <= 8192
            ):
                self.exit_code, self.children = code, children
            else:
                raise ManagedRuntimeError("Conflicting owned exec lifecycle record")
        return bytes(output)


class OwnedDockerProcess:
    """SDK process interface. Native completion and resource release are separate."""

    def __init__(
        self, runtime: OwnedExecRuntime, handle, connection, *, control=None, on_broken=None
    ):
        self.runtime, self.handle, self.connection = runtime, handle, connection
        self.socket = _raw(connection)
        self.socket.setblocking(False)
        self.stdin, self.stdout = _Input(self), _Output()
        self.records = SupervisorOutput(handle.control_key)
        self.returncode = None
        self.stderr, self.stderr_truncated = bytearray(), False
        self.environment_broken = False
        self._control_callback, self._broken_callback = control, on_broken
        self.safety_task = None
        self._ready, self._done = asyncio.Event(), asyncio.Event()
        self._stop_tasks = []
        self._pump = asyncio.create_task(self._read())

    @classmethod
    async def start(cls, runtime, handle, spec, *, control=None, on_broken=None):
        connection = await asyncio.to_thread(runtime.start, handle, spec)
        process = cls(runtime, handle, connection, control=control, on_broken=on_broken)
        try:
            await asyncio.wait_for(process._ready.wait(), 15)
            if not process.records.ready:
                raise ManagedRuntimeError("Owned exec did not become ready")
            return process
        except BaseException:
            with suppress(Exception):
                await asyncio.to_thread(runtime.control, handle, "stop")
            await process.aclose()
            raise

    async def _exact(self, count):
        result = bytearray()
        while len(result) < count:
            value = await asyncio.get_running_loop().sock_recv(self.socket, count - len(result))
            if not value:
                raise ManagedRuntimeError("Owned exec stream ended without native completion")
            result.extend(value)
        return bytes(result)

    async def _read(self):
        try:
            while self.records.exit_code is None:
                header = await self._exact(8)
                size = int.from_bytes(header[4:], "big")
                if header[0] not in (1, 2) or header[1:4] != b"\0\0\0" or size > 2 * MAX_RECORD:
                    raise ManagedRuntimeError("Invalid owned exec stream frame")
                data = await self._exact(size)
                if header[0] == 1:
                    value = self.records.feed(data)
                    if self.records.ready:
                        self._ready.set()
                    if value:
                        await self.stdout.queue.put(value)
                else:
                    remaining = max(0, 65536 - len(self.stderr))
                    self.stderr.extend(data[:remaining])
                    self.stderr_truncated |= len(data) > remaining
            self.returncode = self.records.exit_code
        except asyncio.CancelledError:
            return
        except Exception:
            self.environment_broken = True
            self.stdout.error = ManagedRuntimeError(
                "Owned exec transport failed; environment is unsafe to grade"
            )
            # A lost supervisor cannot prove descendant ownership any longer.
            # Stop this owned environment and retain its stopped filesystem.
            if self._broken_callback is not None:
                # A broker callback acquires its own current-generation fence.
                # Do not wait while the failed startup operation still holds it.
                self.safety_task = asyncio.ensure_future(self._broken_callback())
                self.safety_task.add_done_callback(
                    lambda task: task.exception() if not task.cancelled() else None
                )
            else:
                with suppress(Exception):
                    await asyncio.to_thread(
                        self.runtime.runtime.stop, self.handle.container, grace_seconds=0
                    )
        finally:
            self._ready.set()
            self._done.set()
            await self.stdout.queue.put(None)

    async def wait(self):
        await self._done.wait()
        if self.stdout.error:
            raise self.stdout.error
        return self.returncode

    def terminate(self):
        self._stop_tasks.append(asyncio.create_task(self._control("stop")))

    def kill(self):
        self.terminate()  # The supervisor already bounds TERM -> KILL for its tree.

    async def release(self):
        return await self._control("release")

    async def _control(self, operation):
        if self._control_callback is not None:
            return await self._control_callback(operation)
        return await asyncio.to_thread(self.runtime.control, self.handle, operation)

    async def aclose(self):
        """Detach transport; the durable environment lease still owns cleanup."""
        self._pump.cancel()
        self.socket.close()
        if self.stdout.queue.full():
            self.stdout.queue.get_nowait()
        with suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(self._pump, *self._stop_tasks, return_exceptions=True), 1
            )
        with suppress(Exception):
            self.connection.close()


class _Input:
    def __init__(self, process):
        self.process, self.buffer = process, bytearray()

    def write(self, data):
        if self.process.returncode is not None or len(self.buffer) + len(data) > MAX_RECORD + 1:
            raise BrokenPipeError("Owned exec command buffer is closed or full")
        self.buffer.extend(data)

    async def drain(self):
        await asyncio.get_running_loop().sock_sendall(self.process.socket, self.buffer)
        self.buffer.clear()

    def close(self):
        with suppress(OSError):
            self.process.socket.shutdown(socket.SHUT_WR)


class _Output:
    def __init__(self):
        self.queue = asyncio.Queue(16)
        self.buffer = bytearray()
        self.eof, self.error = False, None

    async def readline(self):
        while True:
            index = self.buffer.find(b"\n")
            if index > MAX_RECORD or (index < 0 and len(self.buffer) > MAX_RECORD):
                raise ManagedRuntimeError("Owned exec protocol record exceeds size limit")
            if index >= 0:
                result = bytes(self.buffer[: index + 1])
                del self.buffer[: index + 1]
                return result
            if self.eof:
                if self.error:
                    raise self.error
                result, self.buffer = bytes(self.buffer), bytearray()
                return result
            data = await self.queue.get()
            if data is None:
                self.eof = True
            else:
                self.buffer.extend(data)

    async def read(self, size=65536):
        """Bounded raw reads for native commands that are not JSONL workers."""
        if type(size) is not int or not 1 <= size <= MAX_RECORD:
            raise ManagedRuntimeError("Owned exec read size is invalid")
        while not self.buffer and not self.eof:
            data = await self.queue.get()
            if data is None:
                self.eof = True
            else:
                self.buffer.extend(data)
        if self.buffer:
            result = bytes(self.buffer[:size])
            del self.buffer[:size]
            return result
        if self.error:
            raise self.error
        return b""
