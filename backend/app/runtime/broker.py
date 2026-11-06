"""Trusted host grants and durable authority boundary for resource providers.

Benchmark workers submit only plan IDs, opaque handles and commands inside owned
environments. The host resolves registered assets, paths, images and private
inputs. An authority implementation is mandatory; there is no in-memory fallback.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Protocol

from app.runtime.managed_spec import (
    ContainerSpec,
    ExecSpec,
    ManagedContainer,
    ManagedExec,
    ManagedNetwork,
    NetworkRequest,
    OwnerScope,
)


@dataclass(frozen=True)
class ResourceLease:
    handle: str
    scope: OwnerScope
    kind: str
    intent: ContainerSpec | NetworkRequest | ExecSpec = field(repr=False)
    native: ManagedContainer | ManagedNetwork | ManagedExec | None = field(default=None, repr=False)
    state: str = "planned"
    deadline_at: str | None = None


@dataclass(frozen=True)
class ExecutionGrant:
    lease: ResourceLease
    mode: str  # wait or attach
    timeout_seconds: float


@dataclass(frozen=True)
class ExportGrant:
    artifact_id: str
    path: str
    visibility: str
    max_archive_bytes: int
    rootfs: bool = False


class ResourceCall(Protocol):
    scope: OwnerScope
    replay: dict | None

    async def allocation(self, resource_id: str) -> ResourceLease:
        """Resolve the admitted immutable plan and registered assets, not worker paths."""
        ...

    async def resource(self, handle: str) -> ResourceLease:
        """Check the bound scope and phase-specific rights for this opaque handle."""
        ...

    async def execution(self, container: ResourceLease, payload: dict) -> ExecutionGrant:
        """Mint an exec identity/key, resolve private refs and enforce the saved deadline."""
        ...

    async def output(self, container: ResourceLease, output_id: str) -> ExportGrant:
        """Resolve a saved output grant with host-defined path, visibility and byte limit."""
        ...

    async def deadline_expired(self, lease: ResourceLease) -> bool:
        """Compare the current persisted lease deadline with the authority's clock."""
        ...

    async def save(self, lease: ResourceLease) -> None:
        """Durably commit the intent/native state before the next external effect.

        Exec control keys/private inputs require private storage, never public
        snapshots. Scope fencing must remain held across these commits and effects.
        """
        ...

    async def complete(self, result: dict) -> None:
        """Persist the complete operation receipt before returning it or ACKing RPC."""
        ...


class ResourceAuthority(Protocol):
    def operation(self, request: dict) -> AbstractAsyncContextManager[ResourceCall]:
        """Fence the bound worker, owner generation, phase and host reservation.

        Serialize stable request IDs, replay durable receipts and reject changed
        content. Persist the request before yielding. Hold ownership across intent
        commits and native effects. Persist
        failures/uncertainty on exit, including cancellation during blocking I/O.
        Enforce persisted exec deadlines even when an attached transport's caller
        disappears. Finished native processes retain their environment lease for
        grading; a deadline must not kill their retained background services.
        This is supplied by the coordinator; the provider cannot grant itself a
        resource or treat a process-local SDK receipt as durable authority.
        """
        ...


class ArtifactSink(Protocol):
    async def save(
        self, scope: OwnerScope, grant: ExportGrant, chunks: AsyncIterator[bytes]
    ) -> dict:
        """Atomically store bytes, calculate digest/size and return the scoped SDK artifact.

        Failed/oversized transfers must not publish a successful artifact. The
        caller persists the returned reference before making it visible.
        """
        ...
