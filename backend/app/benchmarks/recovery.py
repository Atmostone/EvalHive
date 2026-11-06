"""Recover a trial's scoped resources in reverse dependency order."""

import uuid

from evalhive_benchmark import ProtocolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.benchmarks.recovery_order import dependency_order
from app.benchmarks.resource_authority import SqlResourceAuthority
from app.benchmarks.trials import current_trial, failure
from app.models.benchmark_trial import BenchmarkResourceLease
from app.runtime.managed_spec import OwnerScope
from app.runtime.records import resource_dependencies


def recovery_order(dependencies):
    """Executions precede their parents; namespace users precede the namespace.

    Dependencies already released need no further action. Images and identities
    are still revalidated by the provider under its authority on every operation.
    """
    ordered, parents = dependency_order(dependencies)
    return list(reversed(ordered)), parents


class RecoveryService:
    def __init__(self, store, actor_id, providers):
        """providers maps installed provider IDs to host reconciler factories.

        Factories accept a scoped authority, not credentials from a worker. There
        is no implicit provider fallback and no native benchmark dependency here.
        """
        self.store, self.actor_id, self.providers = store, actor_id, providers

    async def sweep(self, owner, operation="stop"):
        """Quiesce by default. The coordinator applies capture policy before release.

        This method never finishes a trial or silently removes its reservation.
        Failures stay visible and prevent dependent resources being released.
        """
        if operation not in {"inspect", "stop", "release"}:
            raise ValueError("Invalid resource recovery operation")
        async with AsyncSession(self.store.engine, expire_on_commit=False) as db:
            trial, _ = await current_trial(db, owner)
            if trial.phase != "cleaning":
                raise failure("INVALID_PHASE", "Resource recovery requires the cleanup phase")
            rows = (
                await db.scalars(
                    select(BenchmarkResourceLease)
                    .where(
                        BenchmarkResourceLease.trial_id == owner.trial_id,
                        BenchmarkResourceLease.workspace_id == owner.workspace_id,
                        BenchmarkResourceLease.state != "released",
                    )
                    .options(undefer(BenchmarkResourceLease.private_value))
                )
            ).all()
            scopes = {
                str(row.id): OwnerScope(
                    str(row.workspace_id),
                    str(row.trial_id),
                    str(row.session_id),
                    row.origin_generation,
                )
                for row in rows
            }
            dependencies = {
                str(row.id): resource_dependencies(
                    self.store.private.open("lease:" + str(row.id), row.private_value),
                    str(row.id),
                    scopes[str(row.id)],
                )
                for row in rows
            }
            metadata = {str(row.id): (row.session_id, row.provider_id) for row in rows}
        ordered, parents = recovery_order(dependencies)
        blocked, results = set(), []

        def block_ancestors(handle):
            for parent in parents[handle]:
                if parent not in blocked:
                    blocked.add(parent)
                    block_ancestors(parent)

        for handle in ordered:
            if handle in blocked:
                results.append(
                    {
                        "handle": handle,
                        "error": {
                            "code": "DEPENDENCY_UNRESOLVED",
                            "message": "A dependent resource remains unresolved",
                            "kind": "cleanup",
                        },
                    }
                )
                continue
            session_id, provider_id = metadata[handle]
            try:
                factory = self.providers.get(provider_id)
                if factory is None:
                    raise failure(
                        "PROVIDER_UNAVAILABLE",
                        "Installed recovery provider is unavailable",
                        "cleanup",
                    )
                authority = SqlResourceAuthority(
                    self.store, owner, session_id, self.actor_id, provider_id, recovery=True
                )
                reconciler = factory(authority)
                scope = scopes[handle]
                request = {
                    "protocol_version": "1.0",
                    "type": "broker_request",
                    "id": uuid.uuid4().hex,
                    "scope": {
                        "workspace_id": scope.workspace_id,
                        "trial_id": scope.trial_id,
                        "generation": scope.generation,
                    },
                    "provider": provider_id,
                    "operation": operation,
                    "handle": handle,
                    "payload": {"reconcile": True},
                }
                result = await reconciler.handle(request)
                if result.get("handle") != handle:
                    raise failure(
                        "INVALID_RECOVERY", "Provider returned another resource identity", "cleanup"
                    )
                if operation == "release" and result.get("state") != "released":
                    raise failure(
                        "RECOVERY_INCOMPLETE",
                        "Provider has not confirmed resource release",
                        "cleanup",
                    )
                results.append(result)
            except ProtocolError as error:
                if error.code in {
                    "STALE_GENERATION",
                    "OWNER_EXPIRED",
                    "TRIAL_NOT_FOUND",
                    "NO_RESERVATION",
                }:
                    raise
                results.append({"handle": handle, "error": error.as_dict()})
                block_ancestors(handle)
        return results
