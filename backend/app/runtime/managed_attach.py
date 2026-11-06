"""Scoped JSONL container transport shared by benchmark and harness workers."""

import asyncio

from app.runtime.attach import DockerAttachedProcess
from app.runtime.managed_spec import ManagedRuntimeError


class _Binding:
    def __init__(self, runtime, handle):
        self.runtime, self.handle = runtime, handle

    def _check(self, container_id, workspace_id):
        if (
            container_id != self.handle.container_id
            or workspace_id != self.handle.scope.workspace_id
        ):
            raise ManagedRuntimeError("Worker transport belongs to another scope")

    def inspect(self, container_id, workspace_id):
        self._check(container_id, workspace_id)
        return self.runtime.inspect(self.handle)

    def stop(self, container_id, workspace_id, *, grace_seconds):
        self._check(container_id, workspace_id)
        return self.runtime.stop(self.handle, grace_seconds=grace_seconds)


class ManagedDockerProcess(DockerAttachedProcess):
    def __init__(self, runtime, handle, connection, control):
        super().__init__(
            _Binding(runtime, handle), handle.container_id, handle.scope.workspace_id, connection
        )
        self.handle, self.control_callback = handle, control

    @classmethod
    async def attach(cls, runtime, handle, *, control):
        native = await asyncio.to_thread(runtime.container, handle)
        if native is None or native.status != "running":
            raise ManagedRuntimeError("Worker container is unavailable")
        if native.labels.get("evalhive.role") not in {"benchmark.worker", "benchmark.grader"}:
            raise ManagedRuntimeError("Only registered SDK worker containers provide a transport")
        connection = await asyncio.to_thread(
            native.attach_socket,
            params={
                "stdin": 1,
                "stdout": 1,
                "stderr": 1,
                "stream": 1,
                "logs": 0,
            },
        )
        return cls(runtime, handle, connection, control)

    def terminate(self):
        self._stop_tasks.append(asyncio.create_task(self.control_callback("stop")))

    def kill(self):
        self.terminate()
