"""Route SDK resource calls through installed providers and durable ownership."""

import uuid

from evalhive_benchmark import encode_line, validate
from evalhive_benchmark.peer import identity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.benchmarks.resource_authority import SqlResourceAuthority
from app.benchmarks.trials import current_trial, failure
from app.models.benchmark_trial import BenchmarkOperation, BenchmarkResourceLease


class SqlBroker:
    """One registered worker actor, never an actor supplied by an RPC payload.

    Factories receive the scoped SQL authority. Core routing has no knowledge of
    provider implementation, native benchmark modules, paths or images.
    """

    def __init__(self, store, owner, actor_id, providers):
        self.store, self.owner = store, owner
        self.actor_id, self.factories = uuid.UUID(str(actor_id)), dict(providers)
        self.instances = {}

    async def _route(self, request, *, replay=False):
        validate("broker_request", request)
        encode_line(request)
        if request["scope"] != {
            "workspace_id": str(self.owner.workspace_id),
            "trial_id": str(self.owner.trial_id),
            "generation": self.owner.generation,
        }:
            raise failure("STALE_GENERATION", "Broker request is outside its worker scope")
        async with AsyncSession(self.store.engine) as db:
            trial, _ = await current_trial(db, self.owner)
            if trial.phase == "finished":
                raise failure("TRIAL_FINISHED", "Broker trial has finished")
            # Look up the original session BEFORE considering a current logical
            # resource name. Released/replaced resources never redirect retries.
            previous = await db.scalar(
                select(BenchmarkOperation).where(
                    BenchmarkOperation.trial_id == trial.id,
                    BenchmarkOperation.generation == self.owner.generation,
                    BenchmarkOperation.actor_id == self.actor_id,
                    BenchmarkOperation.request_id == request["id"],
                )
            )
            if previous is not None:
                if previous.content_hash != identity(request):
                    raise failure("COMMAND_CONFLICT", "Broker request ID has different content")
                session_id = previous.session_id
            elif replay:
                raise failure("RECEIPT_UNAVAILABLE", "Broker receipt is unavailable", "uncertain")
            else:
                statement = select(BenchmarkResourceLease.session_id).where(
                    BenchmarkResourceLease.trial_id == trial.id,
                    BenchmarkResourceLease.workspace_id == self.owner.workspace_id,
                    BenchmarkResourceLease.origin_generation == self.owner.generation,
                    BenchmarkResourceLease.provider_id == request["provider"],
                )
                if request["operation"] == "allocate" and "handle" not in request:
                    statement = statement.where(
                        BenchmarkResourceLease.logical_id == request["payload"].get("resource_id"),
                        BenchmarkResourceLease.state != "released",
                    )
                else:
                    try:
                        handle = uuid.UUID(request.get("handle", ""))
                    except (ValueError, TypeError, AttributeError):
                        raise failure("FOREIGN_HANDLE", "Resource handle is unavailable") from None
                    statement = statement.where(BenchmarkResourceLease.id == handle)
                sessions = (await db.scalars(statement.limit(2))).all()
                if len(sessions) != 1:
                    raise failure("UNREGISTERED_RESOURCE", "Resource is unavailable or ambiguous")
                session_id = sessions[0]
        key = (session_id, request["provider"])
        if key not in self.instances:
            factory = self.factories.get(request["provider"])
            if factory is None:
                raise failure(
                    "PROVIDER_UNAVAILABLE", "Installed provider is unavailable", "preparation"
                )
            if len(self.instances) >= 128:
                raise failure("PROVIDER_LIMIT", "Trial provider session limit reached")
            authority = SqlResourceAuthority(
                self.store, self.owner, session_id, self.actor_id, request["provider"]
            )
            self.instances[key] = factory(authority)
        return self.instances[key]

    async def handle(self, request):
        return await (await self._route(request)).handle(request)

    async def replay(self, request):
        """A retired IPC receipt may only read a committed SQL result."""
        return await (await self._route(request, replay=True)).replay(request)

    async def open_transport(self, request):
        """Host API; never exposed as a worker broker operation."""
        return await (await self._route(request)).open_transport(request)

    async def close(self):
        for provider in self.instances.values():
            await provider.close()
