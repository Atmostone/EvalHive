"""Rootless Docker process runtime, independent of prompts, models and harness APIs."""

from __future__ import annotations

import re
import uuid
from pathlib import PurePosixPath

import docker
from docker.errors import ImageNotFound, NotFound

from app.config import get_settings
from app.harnesses.catalog import get_manifest
from app.runtime.base import ProcessRuntime
from app.runtime.spec import ProcessHandle, ProcessSpec

LABEL = "evalhive"


class RuntimeFailure(ValueError):
    """A public runtime error whose message never includes private process input."""


def registered_image(manifest, environment_id):
    images = manifest.get("requirements", {}).get("environment_images", {})
    return images.get(environment_id, manifest["image"])


class DockerProcessRuntime(ProcessRuntime):
    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def _check_daemon(self):
        if not any(
            option == "name=rootless" for option in self.client.info().get("SecurityOptions", [])
        ):
            raise RuntimeFailure("Execution requires the EvalHive rootless Docker daemon")

    def resolve_image(
        self, connector_id: str, connector_version: str, environment_id: str = "workspace"
    ) -> str:
        manifest = get_manifest(connector_id, connector_version)
        self._check_daemon()
        try:
            return self.client.images.get(registered_image(manifest, environment_id)).id
        except ImageNotFound:
            raise RuntimeFailure("Registered connector image is not installed") from None

    @staticmethod
    def _ids(spec):
        try:
            return tuple(
                str(uuid.UUID(value))
                for value in (spec.workspace_id, spec.task_id, spec.attempt_id)
            )
        except (ValueError, TypeError, AttributeError):
            raise RuntimeFailure("Process scope identifiers must be UUIDs") from None

    def _network(self, spec):
        network = spec.network
        if sum([network.bridge is not None, network.container is not None, network.disabled]) > 1:
            raise RuntimeFailure("Select exactly one network mode")
        if network.disabled:
            return {"network_disabled": True}
        if network.container is not None:
            try:
                target = self.client.containers.get(network.container)
            except NotFound:
                raise RuntimeFailure("Task environment network container is unavailable") from None
            if target.labels.get(f"{LABEL}.role") != "environment" or (
                target.labels.get(f"{LABEL}.task_id") != spec.task_id
                or target.labels.get(f"{LABEL}.workspace_id") != spec.workspace_id
            ):
                raise RuntimeFailure("Network namespace does not belong to this task environment")
            if target.status != "running":
                raise RuntimeFailure("Task environment network container is not running")
            return {"network_mode": "container:" + target.id}
        bridge = network.bridge or get_settings().runtime_network
        if bridge != get_settings().runtime_network:
            raise RuntimeFailure("Network is not registered for this runtime")
        return {"network": bridge}

    def spawn(self, spec: ProcessSpec) -> ProcessHandle:
        workspace_id, task_id, attempt_id = self._ids(spec)
        if not spec.argv or not all(
            isinstance(arg, str) and arg and "\x00" not in arg for arg in spec.argv
        ):
            raise RuntimeFailure("Process argv must contain nonempty strings")
        if (
            not PurePosixPath(spec.workdir).is_absolute()
            or ".." in PurePosixPath(spec.workdir).parts
        ):
            raise RuntimeFailure("Process working directory must be absolute")
        resources = spec.resources
        if (
            not re.fullmatch(r"[1-9][0-9]*(?:[kmgt])?", resources.memory.lower())
            or not 1000 <= resources.cpu_quota <= get_settings().runtime_max_cpu_quota
            or not 1 <= resources.pids <= 1024
        ):
            raise RuntimeFailure("Invalid runtime resource limits")
        memory = re.fullmatch(r"([1-9][0-9]*)([kmgt])?", resources.memory.lower())
        memory_bytes = (
            int(memory[1])
            * {None: 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[memory[2]]
        )
        if not 6 * 1024**2 <= memory_bytes <= get_settings().runtime_max_memory_mb * 1024**2:
            raise RuntimeFailure("Process memory is outside runtime policy")
        mounts = {}
        targets = set()
        host_data = PurePosixPath(get_settings().host_data_dir)
        if not host_data.is_absolute():
            raise RuntimeFailure("Runtime requires an absolute host data directory")
        attempt_root = host_data / "workspaces" / task_id / attempt_id
        shared_root = host_data / "shared" / workspace_id
        readonly_roots = [PurePosixPath(root) for root in get_settings().runtime_readonly_roots]
        for mount in spec.mounts:
            source, target = PurePosixPath(mount.source), PurePosixPath(mount.target)
            if (
                not source.is_absolute()
                or not target.is_absolute()
                or ".." in source.parts
                or ".." in target.parts
            ):
                raise RuntimeFailure("Mount paths must be absolute and contained")
            if str(target) in targets or target == PurePosixPath("/"):
                raise RuntimeFailure(
                    "Mount targets must be distinct and cannot replace the container root"
                )
            targets.add(str(target))
            if str(source) in mounts:
                raise RuntimeFailure("A host source may only be mounted once")
            if not source.is_relative_to(attempt_root):
                if not mount.read_only or not any(
                    source.is_relative_to(root) for root in [shared_root, *readonly_roots]
                ):
                    raise RuntimeFailure("Host mount is outside the registered task environment")
            mounts[str(source)] = {"bind": str(target), "mode": "ro" if mount.read_only else "rw"}
        manifest = get_manifest(spec.connector_id, spec.connector_version)
        self._check_daemon()
        try:
            image = self.client.images.get(registered_image(manifest, spec.environment_id))
        except ImageNotFound:
            raise RuntimeFailure("Registered connector image is not installed") from None
        if spec.image_id is not None and image.id != spec.image_id:
            raise RuntimeFailure("Registered image changed since execution preflight")
        labels = {
            f"{LABEL}.role": "runner",
            f"{LABEL}.workspace_id": workspace_id,
            f"{LABEL}.task_id": task_id,
            f"{LABEL}.attempt_id": attempt_id,
            f"{LABEL}.connector_id": manifest["id"],
            f"{LABEL}.connector_version": manifest["version"],
        }
        container = self.client.containers.run(
            image=image.id,
            entrypoint=list(spec.argv),
            command=[],
            working_dir=spec.workdir,
            name=f"evalhive-run-{attempt_id[:8]}-{uuid.uuid4().hex[:8]}",
            environment=dict(spec.environment),
            volumes=mounts,
            mem_limit=resources.memory,
            cpu_period=100000,
            cpu_quota=resources.cpu_quota,
            pids_limit=resources.pids,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            init=True,
            stdin_open=True,
            tty=False,
            log_config=docker.types.LogConfig(type="none"),
            detach=True,
            labels=labels,
            **self._network(spec),
        )
        return ProcessHandle(container.id, image.id)

    def _owned(self, container_id: str, workspace_id: str):
        self._check_daemon()
        try:
            container = self.client.containers.get(container_id)
        except NotFound:
            return None
        if container.labels.get(f"{LABEL}.role") != "runner" or container.labels.get(
            f"{LABEL}.workspace_id"
        ) != str(workspace_id):
            return None
        return container

    def stop(
        self, container_id: str, workspace_id: str, *, grace_seconds: int = 10, remove: bool = False
    ) -> bool:
        container = self._owned(container_id, workspace_id)
        if container is None:
            return False
        container.stop(timeout=max(0, min(grace_seconds, 30)))
        if remove:
            container.remove(force=True)
        return True

    def inspect(self, container_id: str, workspace_id: str) -> dict | None:
        container = self._owned(container_id, workspace_id)
        if container is None:
            return None
        container.reload()
        state = container.attrs.get("State", {})
        return {
            "container_id": container.id,
            "status": container.status,
            "image_id": container.attrs.get("Image"),
            "exit_code": state.get("ExitCode"),
            "oom_killed": state.get("OOMKilled"),
            "attempt_id": container.labels.get(f"{LABEL}.attempt_id"),
            "task_id": container.labels.get(f"{LABEL}.task_id"),
            "workspace_id": container.labels.get(f"{LABEL}.workspace_id"),
            "name": container.name,
        }

    def list_active(self, workspace_id: str | None = None) -> list[dict]:
        self._check_daemon()
        labels = [f"{LABEL}.role=runner"]
        if workspace_id is not None:
            labels.append(f"{LABEL}.workspace_id={workspace_id}")
        return [
            {
                "container_id": container.id,
                "task_id": container.labels.get(f"{LABEL}.task_id"),
                "workspace_id": container.labels.get(f"{LABEL}.workspace_id"),
                "attempt_id": container.labels.get(f"{LABEL}.attempt_id"),
                "status": container.status,
            }
            for container in self.client.containers.list(filters={"label": labels})
        ]
