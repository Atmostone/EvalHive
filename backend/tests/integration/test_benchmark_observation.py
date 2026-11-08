"""Native reads release only the effect fence and must revalidate before writes."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.benchmarks.resource_authority import SqlResourceAuthority
from app.benchmarks.trials import TrialStore
from app.models.benchmark_trial import BenchmarkOperation, BenchmarkTrial
from app.runtime.docker_provider import RootlessDockerProvider
from evalhive_benchmark import ProtocolError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tests.integration.test_benchmark_trials import (  # noqa: F401
    MANIFEST,
    WORKSPACE,
    FakeRuntime,
    fixture,
    request,
)


async def test_read_wait_does_not_block_control_operations(fixture):  # noqa: F811
    store, owner, scope, _, lease, authority, _ = fixture
    async with authority.operation(request(scope, "exec", handle=lease.handle)) as call:
        async with call.observation():
            await asyncio.wait_for(store.heartbeat(owner), 2)
            assert await store.request_cancel(WORKSPACE, owner.trial_id)
            async with authority.operation(
                request(scope, "inspect", handle=lease.handle)
            ) as control:
                await control.complete({"control_while_waiting": True})
        await call.complete({"read_finished": True})


async def test_late_observation_cannot_write_into_new_generation(fixture, _engine):  # noqa: F811
    store, owner, scope, _, lease, authority, _ = fixture
    after_claim = None
    with pytest.raises(ProtocolError) as stale:
        async with authority.operation(request(scope, "exec", handle=lease.handle)) as call:
            async with call.observation():
                async with _engine.begin() as connection:
                    await connection.execute(
                        update(BenchmarkTrial)
                        .where(BenchmarkTrial.id == owner.trial_id)
                        .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
                    )
                claimed = await asyncio.wait_for(
                    store.claim(WORKSPACE, owner.trial_id, uuid.uuid4()), 2
                )
                assert claimed.generation == owner.generation + 1
                async with AsyncSession(_engine) as db:
                    after_claim = (await db.get(BenchmarkTrial, owner.trial_id)).next_seq
            pytest.fail("A late observer must revalidate before another effect or receipt")
    assert stale.value.code == "STALE_GENERATION"
    async with AsyncSession(_engine) as db:
        trial = await db.get(BenchmarkTrial, owner.trial_id)
        original = await db.scalar(select(BenchmarkOperation))
        assert trial.next_seq == after_claim and trial.execution_status == "lost"
        assert original.state == "uncertain" and original.error_kind == "uncertain"


async def test_cancelled_observation_releases_the_physical_pool_connection(fixture, _engine):  # noqa: F811
    store, owner, scope, actor, lease, _, _ = fixture
    pooled = create_async_engine(_engine.url, pool_size=1, max_overflow=0)
    authority = SqlResourceAuthority(
        TrialStore(pooled, store.private), owner, scope.session_id, actor, "fixture.rootless"
    )
    entered = asyncio.Event()

    async def waiting():
        async with authority.operation(request(scope, "exec", handle=lease.handle)) as call:
            async with call.observation():
                entered.set()
                await asyncio.Event().wait()

    task = asyncio.create_task(waiting())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # The only pooled connection is reusable during a native read wait, not
        # merely after cancellation. Otherwise parallel waits starve controls.
        await asyncio.wait_for(authority.store.heartbeat(owner), 2)
        async with authority.operation(request(scope, "inspect", handle=lease.handle)) as control:
            await control.complete({"one_connection_control": True})
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        async with authority.operation(request(scope, "inspect", handle=lease.handle)) as call:
            await call.complete({"connection_reused": True})
        async with AsyncSession(_engine) as db:
            assert await db.scalar(
                select(BenchmarkOperation.id).where(BenchmarkOperation.state == "uncertain")
            )
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await pooled.dispose()


async def test_observation_cannot_append_after_the_same_owner_finishes(fixture, _engine):  # noqa: F811
    store, owner, scope, _, lease, authority, _ = fixture
    final_seq = None
    with pytest.raises(ProtocolError) as finished:
        async with authority.operation(request(scope, "exec", handle=lease.handle)) as call:
            async with call.observation():
                await store.transition(owner, "planning", "cleaning")
                await RootlessDockerProvider(FakeRuntime(), authority, MANIFEST).handle(
                    request(scope, "release", handle=lease.handle)
                )
                await store.transition(owner, "cleaning", "finished")
                async with AsyncSession(_engine) as db:
                    final_seq = (await db.get(BenchmarkTrial, owner.trial_id)).next_seq
    assert finished.value.code == "TRIAL_FINISHED"
    async with AsyncSession(_engine) as db:
        assert (await db.get(BenchmarkTrial, owner.trial_id)).next_seq == final_seq
        assert await db.scalar(
            select(BenchmarkOperation.id).where(BenchmarkOperation.state == "uncertain")
        )
