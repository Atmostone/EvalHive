"""Neutral rootless resources. Images and private inputs are resolved by the host."""

from __future__ import annotations

import re
import stat
import uuid
from pathlib import Path, PurePosixPath

import docker
from docker.errors import DockerException, ImageNotFound, NotFound

from app.runtime.managed_spec import (
    ContainerSpec,
    ManagedContainer,
    ManagedNetwork,
    ManagedRuntimeError,
    OwnerScope,
    RuntimePolicy,
    UncertainRuntimeError,
    absolute_path,
)


def _resource_id(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ManagedRuntimeError("Managed resources require UUID identities") from None


class RootlessDockerRuntime:
    """Only trusted host code constructs specs and receives Docker identifiers."""

    def __init__(self, client, policy: RuntimePolicy):
        self.client, self.policy = client, policy

    def check_daemon(self):
        try:
            if "name=rootless" not in self.client.info().get("SecurityOptions", []):
                raise ManagedRuntimeError("Execution requires the EvalHive rootless Docker daemon")
        except DockerException:
            raise ManagedRuntimeError("Rootless Docker is unavailable") from None

    def resolve_image(self, registered_reference: str) -> dict:
        self.check_daemon()
        try:
            image = self.client.images.get(registered_reference)
        except ImageNotFound:
            raise ManagedRuntimeError("Registered resource image is not installed") from None
        except DockerException:
            raise ManagedRuntimeError("Resource image could not be inspected") from None
        if image.attrs.get("Os") != "linux" or image.attrs.get("Architecture") != "amd64":
            raise ManagedRuntimeError("Resource image requires an unsupported platform")
        return {
            "image_id": image.id,
            "os": image.attrs["Os"],
            "architecture": image.attrs["Architecture"],
        }

    @staticmethod
    def _matches(value, scope, resource_id):
        labels = value.attrs.get("Config", {}).get("Labels") or value.attrs.get("Labels") or {}
        return all(
            labels.get(key) == expected
            for key, expected in {
                **scope.labels(),
                "evalhive.resource_id": _resource_id(resource_id),
            }.items()
        )

    def container(self, handle: ManagedContainer):
        self.check_daemon()
        try:
            container = self.client.containers.get(handle.container_id)
            container.reload()
        except NotFound:
            return None
        except DockerException:
            raise ManagedRuntimeError("Owned container could not be inspected") from None
        if (
            not self._matches(container, handle.scope, handle.resource_id)
            or container.attrs.get("Image") != handle.image_id
        ):
            raise ManagedRuntimeError("Container does not belong to this resource generation")
        return container

    def network(self, handle: ManagedNetwork):
        self.check_daemon()
        try:
            network = self.client.networks.get(handle.network_id)
            network.reload()
        except NotFound:
            return None
        except DockerException:
            raise ManagedRuntimeError("Owned network could not be inspected") from None
        if not self._matches(network, handle.scope, handle.resource_id):
            raise ManagedRuntimeError("Network does not belong to this resource generation")
        return network

    def create_network(self, scope: OwnerScope, resource_id: str, *, egress: bool = False):
        if type(egress) is not bool:
            raise ManagedRuntimeError("Network egress policy must be explicit")
        self.check_daemon()
        resource_id = _resource_id(resource_id)
        try:
            network = self.client.networks.create(
                name="evalhive-net-" + resource_id,
                driver="bridge",
                internal=not egress,
                labels={**scope.labels(), "evalhive.resource_id": resource_id},
                check_duplicate=True,
            )
        except DockerException:
            raise UncertainRuntimeError(
                "Network creation needs reconciliation before retry"
            ) from None
        return ManagedNetwork(scope, resource_id, network.id)

    def remove_network(self, handle: ManagedNetwork):
        network = self.network(handle)
        if network is None:
            return False
        try:
            network.remove()
        except DockerException:
            raise UncertainRuntimeError(
                "Network release needs reconciliation before retry"
            ) from None
        return True

    def find_network(self, scope: OwnerScope, resource_id: str) -> ManagedNetwork | None:
        self.check_daemon()
        resource_id = _resource_id(resource_id)
        labels = {**scope.labels(), "evalhive.resource_id": resource_id}
        try:
            values = self.client.networks.list(
                filters={"label": [f"{key}={value}" for key, value in labels.items()]}
            )
        except DockerException:
            raise ManagedRuntimeError("Network reconciliation is unavailable") from None
        if len(values) > 1:
            raise ManagedRuntimeError("Resource identity has conflicting networks")
        if not values:
            return None
        value = values[0]
        if not self._matches(value, scope, resource_id):
            raise ManagedRuntimeError("Reconciled network has a foreign owner")
        return ManagedNetwork(scope, resource_id, value.id)

    def _mounts(self, spec):
        mounted, targets = {}, set()
        scoped = PurePosixPath(self.policy.data.host) / "benchmark-sessions" / spec.scope.session_id
        readonly = [(PurePosixPath(item.host), Path(item.local)) for item in self.policy.readonly]
        for mount in spec.mounts:
            source, target = absolute_path(mount.source), absolute_path(mount.target)
            if (
                target == PurePosixPath("/")
                or any(target.is_relative_to(path) for path in ("/proc", "/sys", "/dev"))
                or any(
                    target.is_relative_to(other) or other.is_relative_to(target)
                    for other in targets
                )
                or str(source) in mounted
            ):
                raise ManagedRuntimeError("Invalid or overlapping managed mount")
            candidates = [
                (
                    scoped,
                    Path(self.policy.data.local) / "benchmark-sessions" / spec.scope.session_id,
                )
            ]
            if mount.read_only:
                candidates += readonly
            mapping = next(
                ((host, local) for host, local in candidates if source.is_relative_to(host)), None
            )
            if mapping is None:
                raise ManagedRuntimeError("Host mount is outside the registered resource roots")
            host, local = mapping
            value = local / source.relative_to(host)
            try:
                if (
                    local.resolve(strict=True) != local
                    or value.resolve(strict=True) != value
                    or not value.resolve().is_relative_to(local)
                    or stat.S_ISSOCK(value.stat().st_mode)
                ):
                    raise ManagedRuntimeError(
                        "Managed mounts cannot traverse links or expose sockets"
                    )
            except OSError:
                raise ManagedRuntimeError("Managed mount source is unavailable") from None
            mounted[str(source)] = {"bind": str(target), "mode": "ro" if mount.read_only else "rw"}
            targets.add(target)
        return mounted

    def create(self, spec: ContainerSpec) -> ManagedContainer:
        resource_id = _resource_id(spec.resource_id)
        spec.quota.validate(self.policy)
        if spec.role not in {"environment", "service", "worker", "grader"}:
            raise ManagedRuntimeError("Unsupported managed process role")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", spec.image_id):
            raise ManagedRuntimeError("Managed execution requires an immutable image ID")
        for argv in (spec.entrypoint, spec.command):
            if argv is not None and any(
                not isinstance(arg, str) or not arg or "\0" in arg for arg in argv
            ):
                raise ManagedRuntimeError("Process arguments must be nonempty strings")
        if spec.workdir is not None:
            absolute_path(spec.workdir)
        if any(
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\0" in key
            or not isinstance(value, str)
            or "\0" in value
            for key, value in spec.environment.items()
        ):
            raise ManagedRuntimeError("Invalid process environment")
        if spec.network is not None and spec.namespace is not None:
            raise ManagedRuntimeError("Select one managed network mode")
        network = {"network_disabled": True}
        if spec.network is not None:
            if spec.network.scope != spec.scope or self.network(spec.network) is None:
                raise ManagedRuntimeError("Network does not belong to this environment session")
            network = {"network": spec.network.network_id}
        if spec.namespace is not None:
            if spec.namespace.scope != spec.scope:
                raise ManagedRuntimeError(
                    "Network namespace does not belong to this environment session"
                )
            parent = self.container(spec.namespace)
            if parent is None or parent.status != "running":
                raise ManagedRuntimeError("Environment network namespace is unavailable")
            if parent.labels.get("evalhive.role") not in {
                "benchmark.environment",
                "benchmark.service",
            }:
                raise ManagedRuntimeError("Network namespace is not a task environment")
            network = {"network_mode": "container:" + parent.id}
        mounts = self._mounts(spec)
        if self.resolve_image(spec.image_id)["image_id"] != spec.image_id:
            raise ManagedRuntimeError("Resource image changed since admission")
        try:
            container = self.client.containers.create(
                image=spec.image_id,
                name="evalhive-resource-" + resource_id,
                entrypoint=list(spec.entrypoint) if spec.entrypoint is not None else None,
                command=list(spec.command) if spec.command is not None else None,
                working_dir=spec.workdir,
                user=spec.user,
                environment=dict(spec.environment),
                volumes=mounts,
                mem_limit=f"{spec.quota.memory_mb}m",
                cpu_period=100000,
                cpu_quota=int(spec.quota.cpu_cores * 100000),
                pids_limit=spec.quota.pids,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                init=True,
                stdin_open=True,
                tty=False,
                read_only=spec.readonly_root,
                log_config=docker.types.LogConfig(type="none"),
                labels={
                    **spec.scope.labels(),
                    "evalhive.resource_id": resource_id,
                    "evalhive.role": "benchmark." + spec.role,
                },
                **network,
            )
            container.start()
        except DockerException:
            # Creation/start may have happened. The host retains the intent and
            # finds this deterministic name/labels instead of blindly recreating.
            raise UncertainRuntimeError(
                "Container creation needs reconciliation before retry"
            ) from None
        return ManagedContainer(spec.scope, resource_id, container.id, spec.image_id)

    def find(self, scope: OwnerScope, resource_id: str) -> ManagedContainer | None:
        self.check_daemon()
        resource_id = _resource_id(resource_id)
        labels = {**scope.labels(), "evalhive.resource_id": resource_id}
        try:
            containers = self.client.containers.list(
                all=True, filters={"label": [f"{key}={value}" for key, value in labels.items()]}
            )
        except DockerException:
            raise ManagedRuntimeError("Resource reconciliation is unavailable") from None
        if len(containers) > 1:
            raise ManagedRuntimeError("Resource identity has conflicting containers")
        if not containers:
            return None
        value = containers[0]
        if not self._matches(value, scope, resource_id):
            raise ManagedRuntimeError("Reconciled resource has a foreign owner")
        return ManagedContainer(scope, resource_id, value.id, value.attrs["Image"])

    def inspect(self, handle: ManagedContainer) -> dict | None:
        container = self.container(handle)
        if container is None:
            return None
        state = container.attrs.get("State", {})
        return {
            "status": state.get("Status", container.status),
            "exit_code": state.get("ExitCode"),
            "image_id": handle.image_id,
            "resource_id": handle.resource_id,
        }

    def stop(self, handle: ManagedContainer, *, grace_seconds: int = 10, remove: bool = False):
        container = self.container(handle)
        if container is None:
            return False
        try:
            if container.status == "paused":
                container.unpause()
            if container.status in {"running", "paused", "restarting"}:
                container.stop(timeout=max(0, min(grace_seconds, 30)))
            if remove:
                container.remove(force=True, v=True)
        except DockerException:
            raise UncertainRuntimeError(
                "Container release needs reconciliation before retry"
            ) from None
        return True
