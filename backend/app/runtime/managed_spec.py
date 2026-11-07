"""Server-owned process declarations; public workers never supply host paths."""

from __future__ import annotations

import math
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from app.runtime.spec import BindMount


class ManagedRuntimeError(ValueError):
    """Public diagnostics must not include private process inputs."""


class UncertainRuntimeError(ManagedRuntimeError):
    """Reconcile the durable intent before retrying a mutating runtime operation."""


CONTAINER_CAPABILITIES = frozenset(
    {
        "AUDIT_WRITE",
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "FSETID",
        "KILL",
        "MKNOD",
        "NET_BIND_SERVICE",
        "NET_RAW",
        "SETFCAP",
        "SETGID",
        "SETPCAP",
        "SETUID",
        "SYS_CHROOT",
    }
)


def container_capabilities(values):
    if (
        not isinstance(values, (tuple, list))
        or len(values) > len(CONTAINER_CAPABILITIES)
        or any(
            not isinstance(value, str) or value not in CONTAINER_CAPABILITIES for value in values
        )
        or len(set(values)) != len(values)
    ):
        raise ManagedRuntimeError("Unsupported container Linux capabilities")
    return tuple(sorted(values))


def absolute_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ManagedRuntimeError("Invalid runtime path")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ManagedRuntimeError("Runtime paths must be absolute and contained")
    return path


@dataclass(frozen=True)
class OwnerScope:
    workspace_id: str
    trial_id: str
    session_id: str
    generation: int

    def __post_init__(self):
        try:
            for name in ("workspace_id", "trial_id", "session_id"):
                object.__setattr__(self, name, str(uuid.UUID(str(getattr(self, name)))))
        except (ValueError, TypeError, AttributeError):
            raise ManagedRuntimeError("Runtime ownership requires UUID identities") from None
        if type(self.generation) is not int or self.generation < 1:
            raise ManagedRuntimeError("Runtime ownership requires a positive generation")

    def labels(self) -> dict[str, str]:
        return {
            "evalhive.managed": "benchmark-sdk-v1",
            "evalhive.workspace_id": self.workspace_id,
            "evalhive.trial_id": self.trial_id,
            "evalhive.session_id": self.session_id,
            "evalhive.generation": str(self.generation),
        }


@dataclass(frozen=True)
class PathMapping:
    host: str
    local: str

    def __post_init__(self):
        if absolute_path(self.host) == PurePosixPath("/") or absolute_path(
            self.local
        ) == PurePosixPath("/"):
            raise ManagedRuntimeError("A managed path mapping cannot expose the filesystem root")


@dataclass(frozen=True)
class RuntimePolicy:
    data: PathMapping
    readonly: tuple[PathMapping, ...] = ()
    max_memory_mb: int = 2048
    max_cpu_cores: float = 1.0
    max_pids: int = 1024
    allowed_capabilities: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(
            self, "allowed_capabilities", container_capabilities(self.allowed_capabilities)
        )


@dataclass(frozen=True)
class Quota:
    memory_mb: int = 2048
    cpu_cores: float = 1.0
    pids: int = 256

    def validate(self, policy: RuntimePolicy):
        if (
            type(self.memory_mb) is not int
            or not 6 <= self.memory_mb <= policy.max_memory_mb
            or isinstance(self.cpu_cores, bool)
            or not isinstance(self.cpu_cores, (int, float))
            or not math.isfinite(self.cpu_cores)
            or not 0.01 <= self.cpu_cores <= policy.max_cpu_cores
            or type(self.pids) is not int
            or not 1 <= self.pids <= policy.max_pids
        ):
            raise ManagedRuntimeError("Native resource requirements exceed runtime policy")


@dataclass(frozen=True)
class ManagedNetwork:
    scope: OwnerScope
    resource_id: str
    network_id: str


@dataclass(frozen=True)
class NetworkRequest:
    scope: OwnerScope
    resource_id: str
    egress: bool = False


@dataclass(frozen=True)
class ManagedContainer:
    scope: OwnerScope
    resource_id: str
    container_id: str
    image_id: str


@dataclass(frozen=True)
class VolumeRequest:
    scope: OwnerScope
    resource_id: str
    disk_mb: int

    def __post_init__(self):
        if type(self.disk_mb) is not int or not 1 <= self.disk_mb <= 2**53 - 1:
            raise ManagedRuntimeError("Owned storage requires a positive disk reservation")


@dataclass(frozen=True)
class ManagedVolume:
    scope: OwnerScope
    resource_id: str
    name: str


@dataclass(frozen=True)
class VolumeMount:
    volume: ManagedVolume
    target: str
    read_only: bool = False
    no_copy: bool = True

    def __post_init__(self):
        path = absolute_path(self.target)
        if (
            path == PurePosixPath("/")
            or type(self.read_only) is not bool
            or type(self.no_copy) is not bool
            or any(path.is_relative_to(root) for root in ("/proc", "/sys", "/dev"))
        ):
            raise ManagedRuntimeError("Owned storage requires a contained mount target")


@dataclass(frozen=True)
class ContainerSpec:
    scope: OwnerScope
    resource_id: str
    image_id: str
    role: str = "environment"
    # None preserves the image's native startup behavior.
    entrypoint: tuple[str, ...] | None = None
    command: tuple[str, ...] | None = None
    workdir: str | None = None
    user: str | None = None
    environment: dict[str, str] = field(default_factory=dict, repr=False)
    mounts: tuple[BindMount, ...] = ()
    quota: Quota = field(default_factory=Quota)
    network: ManagedNetwork | None = None
    network_aliases: tuple[str, ...] = ()
    namespace: ManagedContainer | None = None
    readonly_root: bool = False
    storage: tuple[VolumeMount, ...] = ()
    capabilities: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "capabilities", container_capabilities(self.capabilities))


@dataclass(frozen=True)
class ExecSpec:
    """Trusted host declaration. Persist the control key before creating an exec."""

    container: ManagedContainer
    resource_id: str
    argv: tuple[str, ...] = field(repr=False)
    workdir: str | None = None
    user: str | None = None
    environment: dict[str, str] = field(default_factory=dict, repr=False)
    private_inputs: dict[str, bytes] = field(default_factory=dict, repr=False)
    control_key: bytes = field(default_factory=lambda: secrets.token_bytes(32), repr=False)


@dataclass(frozen=True)
class ManagedExec:
    container: ManagedContainer
    resource_id: str
    exec_id: str
    argv: tuple[str, ...] = field(repr=False)
    control_key: bytes = field(repr=False)
    user: str | None = None
