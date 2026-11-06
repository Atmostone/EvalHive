"""Neutral process, filesystem, resource and network declarations."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BindMount:
    source: str
    target: str
    read_only: bool = True


@dataclass(frozen=True)
class ResourceLimits:
    memory: str = "2g"
    cpu_quota: int = 100000
    pids: int = 256


@dataclass(frozen=True)
class NetworkSpec:
    bridge: str | None = None
    container: str | None = None
    disabled: bool = False


@dataclass(frozen=True)
class ProcessSpec:
    task_id: str
    workspace_id: str
    attempt_id: str
    connector_id: str
    connector_version: str
    argv: tuple[str, ...]
    environment_id: str = "workspace"
    image_id: str | None = None
    workdir: str = "/workspace"
    environment: dict[str, str] = field(default_factory=dict, repr=False)
    mounts: tuple[BindMount, ...] = ()
    resources: ResourceLimits = field(default_factory=ResourceLimits)
    network: NetworkSpec = field(default_factory=NetworkSpec)


@dataclass(frozen=True)
class ProcessHandle:
    container_id: str
    image_id: str
