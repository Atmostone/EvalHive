"""Host deadline sweep for durable attached execs, independent of their caller."""

import asyncio
import uuid

from evalhive_benchmark import ProtocolError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.benchmarks.resource_authority import SqlResourceAuthority
from app.models.benchmark_trial import BenchmarkEnvironmentSession, BenchmarkResourceLease


class DeadlineService:
    def __init__(self, store, provider_id, actor_id, reconciler_factory):
        self.store, self.reconciler_factory = store, reconciler_factory
        self.provider_id, self.actor_id = provider_id, actor_id

    async def sweep(self, owner):
        # Discovery is read-only. Every native observation/effect re-enters the
        # authority and rechecks owner, phase, host reservation and DB deadline.
        async with AsyncSession(self.store.engine, expire_on_commit=False) as db:
            await self.store.current(db, owner)
            rows = (
                await db.execute(
                    select(
                        BenchmarkResourceLease.id,
                        BenchmarkResourceLease.session_id,
                    )
                    .join(BenchmarkEnvironmentSession)
                    .where(
                        BenchmarkEnvironmentSession.control_id == owner.control_id,
                        BenchmarkResourceLease.trial_id == owner.trial_id,
                        BenchmarkResourceLease.workspace_id == owner.workspace_id,
                        BenchmarkResourceLease.origin_generation == owner.generation,
                        BenchmarkResourceLease.provider_id == self.provider_id,
                        BenchmarkResourceLease.kind == "exec",
                        BenchmarkResourceLease.state.in_(
                            ["creating", "created", "running", "unsafe", "expiring"]
                        ),
                        BenchmarkResourceLease.deadline_at <= func.clock_timestamp(),
                    )
                    .order_by(BenchmarkResourceLease.deadline_at, BenchmarkResourceLease.id)
                )
            ).all()
        results = []
        for handle, session_id in rows:
            authority = SqlResourceAuthority(
                self.store, owner, session_id, self.actor_id, self.provider_id
            )
            reconciler = self.reconciler_factory(authority)
            request = {
                "protocol_version": "1.0",
                "type": "broker_request",
                "id": uuid.uuid4().hex,
                "scope": {
                    "workspace_id": str(owner.workspace_id),
                    "trial_id": str(owner.trial_id),
                    "generation": owner.generation,
                },
                "provider": self.provider_id,
                "operation": "stop",
                "handle": str(handle),
                "payload": {"expire": True},
            }
            try:
                results.append(await reconciler.handle(request))
            except ProtocolError as error:
                if error.code in {
                    "STALE_GENERATION",
                    "OWNER_EXPIRED",
                    "TRIAL_NOT_FOUND",
                    "NO_RESERVATION",
                }:
                    raise
                results.append({"handle": str(handle), "error": error.as_dict()})
        return results

    async def run(self, owner, stop, on_result, *, interval=0.5):
        """The controller observes this task and runs its heartbeat separately.

        on_result must durably project outcomes/errors before the next sweep.
        A failure is propagated to the controller, never silently discarded.
        """
        if not 0.05 <= interval <= 10:
            raise ValueError("Deadline sweep interval must be between 0.05 and 10 seconds")
        while not stop.is_set():
            for result in await self.sweep(owner):
                await on_result(result)
            try:
                await asyncio.wait_for(stop.wait(), interval)
            except TimeoutError:
                pass
