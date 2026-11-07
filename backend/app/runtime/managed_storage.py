"""Label-fenced local Docker storage; native workers never select host paths."""

import uuid

from docker.errors import DockerException, NotFound

from app.runtime.managed_spec import (
    ManagedRuntimeError,
    ManagedVolume,
    UncertainRuntimeError,
    VolumeMount,
    VolumeRequest,
    absolute_path,
)


class RootlessVolumeRuntime:
    def __init__(self, runtime):
        self.runtime, self.client = runtime, runtime.client

    @staticmethod
    def name(resource_id):
        try:
            normalized = str(uuid.UUID(resource_id))
        except (ValueError, TypeError, AttributeError):
            raise ManagedRuntimeError("Owned storage requires a UUID resource identity") from None
        if normalized != resource_id:
            raise ManagedRuntimeError("Owned storage identity must be canonical")
        return "evalhive-storage-" + normalized

    @staticmethod
    def labels(scope, resource_id):
        return {
            **scope.labels(),
            "evalhive.resource_id": resource_id,
            "evalhive.role": "benchmark.storage",
        }

    def find(self, scope, resource_id):
        name = self.name(resource_id)
        self.runtime.check_daemon()
        try:
            volume = self.client.volumes.get(name)
        except NotFound:
            return None
        attrs = volume.attrs
        labels = attrs.get("Labels") or {}
        if (
            attrs.get("Name") != name
            or attrs.get("Driver") != "local"
            or attrs.get("Scope") != "local"
            or attrs.get("Options")
            or any(
                labels.get(key) != value for key, value in self.labels(scope, resource_id).items()
            )
        ):
            raise ManagedRuntimeError("Native storage differs from its owned local declaration")
        return ManagedVolume(scope, resource_id, name)

    def get(self, handle):
        if not isinstance(handle, ManagedVolume) or handle.name != self.name(handle.resource_id):
            raise ManagedRuntimeError("Native storage handle is invalid")
        return self.find(handle.scope, handle.resource_id)

    def create(self, spec):
        if not isinstance(spec, VolumeRequest):
            raise ManagedRuntimeError("Owned storage requires a host declaration")
        name = self.name(spec.resource_id)
        self.runtime.check_daemon()
        if self.find(spec.scope, spec.resource_id) is not None:
            raise ManagedRuntimeError("Existing storage requires reconciliation")
        try:
            self.client.volumes.create(
                name=name, driver="local", labels=self.labels(spec.scope, spec.resource_id)
            )
            result = self.find(spec.scope, spec.resource_id)
            if result is None:
                raise UncertainRuntimeError("Created storage is not observable")
            return result
        except (DockerException, ManagedRuntimeError):
            raise UncertainRuntimeError("Storage creation requires reconciliation") from None

    def remove(self, handle):
        if self.get(handle) is None:
            return
        try:
            # Docker rejects removal while any container still references it.
            self.client.volumes.get(handle.name).remove(force=False)
            if self.get(handle) is not None:
                raise UncertainRuntimeError("Storage removal has not completed")
        except NotFound:
            return
        except DockerException:
            raise UncertainRuntimeError("Storage removal requires reconciliation") from None

    def bindings(self, spec):
        mounts, targets = {}, [absolute_path(mount.target) for mount in spec.mounts]
        if not isinstance(spec.storage, tuple):
            raise ManagedRuntimeError("Owned storage mounts require a normalized declaration")
        for mount in spec.storage:
            if (
                not isinstance(mount, VolumeMount)
                or mount.volume.scope != spec.scope
                or self.get(mount.volume) is None
            ):
                raise ManagedRuntimeError("Container storage is outside its environment session")
            target = absolute_path(mount.target)
            if any(
                target.is_relative_to(other) or other.is_relative_to(target) for other in targets
            ):
                raise ManagedRuntimeError("Owned storage overlaps another container mount")
            if mount.volume.name in mounts:
                raise ManagedRuntimeError("A container cannot mount the same storage twice")
            mounts[mount.volume.name] = {
                "bind": str(target),
                "mode": "ro" if mount.read_only else "rw",
                **({"no_copy": False} if not mount.no_copy else {}),
            }
            targets.append(target)
        return mounts
