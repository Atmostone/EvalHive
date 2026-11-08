"""Measured group capacity checks; the coordinator owns the atomic reservation."""

from __future__ import annotations

import math
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from docker.errors import DockerException
from evalhive_benchmark import ProtocolError, validate

from app.runtime.managed_spec import ManagedRuntimeError, Quota, RuntimePolicy


@dataclass(frozen=True)
class HostCapacity:
    memory_available_mb: int | None
    disk_free_mb: int | None
    cpu_total_cores: int | None
    measured_at: str


@dataclass(frozen=True)
class GroupPolicy:
    max_memory_mb: int = 4096
    max_cpu_cores: float = 2.0
    memory_reserve_mb: int = 512
    disk_reserve_mb: int = 2048
    cpu_reserve_cores: float = 0.5
    max_concurrency: int = 1


@dataclass(frozen=True)
class ResourceDemand:
    """Provider-normalized limits and unchanged minima from registered assets."""

    resource_id: str
    limits: Quota
    minimum_memory_mb: int
    minimum_cpu_cores: float
    disk_mb: int


@dataclass(frozen=True)
class StorageDemand:
    """Persistent workspace storage reserves disk without fictitious CPU/RAM."""

    resource_id: str
    disk_mb: int


def host_capacity(runtime, capacity_path: Path) -> HostCapacity:
    """Host provider only: the storage path is server-owned, never worker input.

    The execution host exposes the actual memory view; unknown measurements fail
    closed. Deployment must point capacity_path at the backing execution storage.
    """
    runtime.check_daemon()
    try:
        info = runtime.client.info()
    except DockerException:
        raise ManagedRuntimeError("Execution host capacity is unavailable") from None
    memory, disk = None, None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                memory = int(line.split()[1]) // 1024
                break
    except (OSError, ValueError, IndexError):
        pass
    try:
        disk = shutil.disk_usage(capacity_path).free // (1024 * 1024)
    except OSError:
        pass
    cores = info.get("NCPU")
    return HostCapacity(
        memory,
        disk,
        cores if type(cores) is int and cores > 0 else None,
        datetime.now(UTC).isoformat(),
    )


def _number(value, minimum=0):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= minimum
    )


def check_group(
    demands: tuple[ResourceDemand | StorageDemand, ...],
    native_resources: dict,
    measured: HostCapacity,
    runtime_policy: RuntimePolicy,
    policy: GroupPolicy,
    *,
    active_trials: int,
    auxiliary_disk_mb: int = 0,
):
    """Recheck under the host reservation lock immediately before preparation.

    This pure result is an eligibility decision, not a lease or launch permission.
    The coordinator must persist the admitted budget before issuing resource RPC.
    """
    if (
        type(active_trials) is not int
        or active_trials < 0
        or type(auxiliary_disk_mb) is not int
        or auxiliary_disk_mb < 0
        or type(policy.max_concurrency) is not int
        or policy.max_concurrency != 1
        or not _number(policy.max_memory_mb, 1)
        or not _number(policy.max_cpu_cores, 0.01)
        or not _number(policy.memory_reserve_mb)
        or not _number(policy.disk_reserve_mb)
        or not _number(policy.cpu_reserve_cores)
    ):
        raise ManagedRuntimeError("Invalid execution group admission policy")
    identities, memory, cpu, disk = set(), 0, 0.0, 0
    reasons = []
    for demand in demands:
        if (
            not isinstance(demand.resource_id, str)
            or not demand.resource_id
            or demand.resource_id in identities
        ):
            raise ManagedRuntimeError("Execution group resources require distinct identities")
        identities.add(demand.resource_id)
        if isinstance(demand, StorageDemand):
            if type(demand.disk_mb) is not int or not 1 <= demand.disk_mb <= 2**53 - 1:
                raise ManagedRuntimeError("Registered storage requirements are invalid")
            disk += demand.disk_mb
            continue
        if not isinstance(demand, ResourceDemand):
            raise ManagedRuntimeError("Execution group demand type is invalid")
        demand.limits.validate(runtime_policy)
        if (
            type(demand.minimum_memory_mb) is not int
            or demand.minimum_memory_mb < 1
            or not _number(demand.minimum_cpu_cores)
            or demand.minimum_cpu_cores <= 0
            or type(demand.disk_mb) is not int
            or demand.disk_mb < 0
        ):
            raise ManagedRuntimeError("Registered resource requirements are invalid")
        if (
            demand.limits.memory_mb < demand.minimum_memory_mb
            or demand.limits.cpu_cores < demand.minimum_cpu_cores
        ):
            reasons.append(
                {"code": "lowered_native_requirements", "resource_id": demand.resource_id}
            )
        memory += demand.limits.memory_mb
        cpu += demand.limits.cpu_cores
        disk += demand.disk_mb
    if not demands:
        reasons.append({"code": "missing_group_resources"})
    required = {"memory_mb": memory, "cpu_cores": cpu, "disk_mb": disk}
    try:
        validate("requirements", {"resources": native_resources})
    except ProtocolError:
        raise ManagedRuntimeError("Native group requirements are invalid") from None
    for name, minimum in native_resources.items():
        if required[name] < minimum:
            reasons.append(
                {
                    "code": "underdeclared_group_requirements",
                    "resource": name,
                    "required": minimum,
                    "declared": required[name],
                }
            )
    # Retained files and their download spool consume capacity, but cannot
    # satisfy a native environment's declared disk requirement.
    required["disk_mb"] += auxiliary_disk_mb
    if active_trials >= policy.max_concurrency:
        reasons.append({"code": "host_busy"})
    capacities = {
        "memory_mb": (measured.memory_available_mb, policy.memory_reserve_mb, policy.max_memory_mb),
        "cpu_cores": (measured.cpu_total_cores, policy.cpu_reserve_cores, policy.max_cpu_cores),
        "disk_mb": (measured.disk_free_mb, policy.disk_reserve_mb, None),
    }
    for name, (available, reserve, maximum) in capacities.items():
        if not _number(available):
            reasons.append({"code": "capacity_unknown", "resource": name})
        elif required[name] + reserve > available or (
            maximum is not None and required[name] > maximum
        ):
            capacity = max(0, available - reserve)
            if maximum is not None:
                capacity = min(capacity, maximum)
            reasons.append(
                {
                    "code": "insufficient_group_resources",
                    "resource": name,
                    "required": required[name],
                    "available": capacity,
                }
            )
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "group_resources": required,
        "measured_at": measured.measured_at,
        "runtime_admission": "reservation_pending",
        "can_run": False,
    }
