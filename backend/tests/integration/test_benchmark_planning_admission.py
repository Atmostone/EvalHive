"""Planning workers cannot prepare native environments before group admission."""

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.benchmarks.private_store import PrivateStore
from app.benchmarks.resource_authority import ActorPolicy, SqlResourceAuthority, install_session
from app.benchmarks.trials import TrialStore
from app.models.benchmark_trial import BenchmarkHostReservation, BenchmarkResourceLease
from app.runtime.admission import GroupPolicy, HostCapacity, ResourceDemand
from app.runtime.broker import ResourceLease
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.managed_spec import ContainerSpec, OwnerScope
from evalhive_benchmark import ProtocolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.test_benchmark_trials import (
    IMAGE,
    MANIFEST,
    POLICY,
    PROVIDER,
    QUOTA,
    WORKSPACE,
    FakeRuntime,
    request,
)

WORKER = ResourceDemand("worker", QUOTA, 256, 0.1, 10)
ENVIRONMENT = ResourceDemand("environment", QUOTA, 256, 0.1, 10)


async def measure():
    return HostCapacity(8192, 8192, 4, datetime.now(UTC).isoformat())


async def admit(store, owner, demands=(WORKER,), *, stage="planning", measuring=measure):
    return await store.reserve(
        owner,
        demands=demands,
        native_resources={},
        measure=measuring,
        runtime_policy=POLICY,
        group_policy=GroupPolicy(),
        stage=stage,
    )


@pytest_asyncio.fixture
async def planning(_engine, db_session):
    store = TrialStore(_engine, PrivateStore("planning-admission-fixture"))
    trial = await store.create(
        WORKSPACE, snapshot={}, deadline_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    owner = await store.claim(WORKSPACE, trial.id, uuid.uuid4())
    await store.transition(owner, "created", "planning")
    await admit(store, owner)
    scope = OwnerScope(str(WORKSPACE), str(trial.id), str(uuid.uuid4()), 1)
    handle, actor = str(uuid.uuid4()), uuid.uuid4()
    lease = ResourceLease(
        handle, scope, "container", ContainerSpec(scope, handle, IMAGE, role="worker", quota=QUOTA)
    )
    return store, owner, scope, actor, lease


async def install(planning, *, selected=None):
    store, owner, scope, actor, lease = planning
    await install_session(
        store,
        owner,
        scope,
        leases={"worker": (PROVIDER, selected or lease)},
        actors={
            actor: ActorPolicy(
                (PROVIDER,),
                ("worker",),
                ("planning", "cleaning"),
                ("allocate", "exec", "inspect", "stop", "release"),
            )
        },
    )
    return SqlResourceAuthority(store, owner, scope.session_id, actor, PROVIDER)


async def test_planning_reservation_only_allows_worker_and_blocks_preparation(planning):
    store, owner, _, _, lease = planning
    with pytest.raises(ProtocolError) as no_preparation:
        await store.transition(owner, "planning", "preparing")
    assert no_preparation.value.code == "GROUP_ADMISSION_REQUIRED"
    with pytest.raises(ProtocolError) as no_environment:
        await install(
            planning, selected=replace(lease, intent=replace(lease.intent, role="environment"))
        )
    assert no_environment.value.code == "PLANNING_WORKER_ONLY"
    await install(planning)


async def test_finalize_preserves_live_worker_and_is_immutable_afterwards(planning, _engine):
    store, owner, scope, _, lease = planning
    authority = await install(planning)
    runtime = FakeRuntime()
    allocate = request(scope, payload={"resource_id": "worker"})
    await RootlessDockerProvider(runtime, authority, MANIFEST).handle(allocate)
    budget = await admit(store, owner, (WORKER, ENVIRONMENT), stage="full")
    assert budget["stage"] == "full" and budget["group_resources"]["memory_mb"] == 512

    async def never_measure():
        pytest.fail("An identical finalized group must use its saved admission receipt")

    assert (
        await admit(store, owner, (WORKER, ENVIRONMENT), stage="full", measuring=never_measure)
        == budget
    )
    with pytest.raises(ProtocolError) as conflict:
        await admit(store, owner, (WORKER,), stage="full")
    assert conflict.value.code == "ADMISSION_CONFLICT"
    with pytest.raises(ProtocolError):
        await admit(store, owner)
    async with AsyncSession(_engine) as db:
        row = await db.get(BenchmarkResourceLease, uuid.UUID(lease.handle))
        assert row.state == "allocated" and runtime.creates == 1
    await store.transition(owner, "planning", "preparing")


@pytest.mark.parametrize(
    "changed", [(), (ENVIRONMENT,), (replace(WORKER, minimum_memory_mb=128), ENVIRONMENT)]
)
async def test_finalize_cannot_drop_or_redefine_the_bootstrap_worker(planning, changed):
    store, owner, _, _, _ = planning
    with pytest.raises(ProtocolError) as conflict:
        await admit(store, owner, changed, stage="full")
    assert conflict.value.code == "ADMISSION_CONFLICT"


async def test_failed_full_admission_keeps_bootstrap_reservation_and_leases(planning, _engine):
    store, owner, _, _, lease = planning
    await install(planning)

    async def insufficient():
        return HostCapacity(256, 8192, 4, datetime.now(UTC).isoformat())

    with pytest.raises(ProtocolError) as capacity:
        await admit(store, owner, (WORKER, ENVIRONMENT), stage="full", measuring=insufficient)
    assert capacity.value.code == "CAPACITY_UNAVAILABLE"
    async with AsyncSession(_engine) as db:
        reservation = await db.get(BenchmarkHostReservation, store.host_id)
        assert reservation.budget["stage"] == "planning"
        assert reservation.budget["resources"][0]["resource_id"] == "worker"
        assert (await db.get(BenchmarkResourceLease, uuid.UUID(lease.handle))).state == "planned"
    with pytest.raises(ProtocolError):
        await store.transition(owner, "planning", "preparing")


async def test_cancel_during_final_admission_preserves_planning_receipt(planning, _engine):
    store, owner, _, _, _ = planning
    entered, resume = asyncio.Event(), asyncio.Event()

    async def delayed():
        entered.set()
        await resume.wait()
        return await measure()

    pending = asyncio.create_task(
        admit(store, owner, (WORKER, ENVIRONMENT), stage="full", measuring=delayed)
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(store.heartbeat(owner), 2)
        assert await asyncio.wait_for(store.request_cancel(WORKSPACE, owner.trial_id), 2)
        resume.set()
        with pytest.raises(ProtocolError) as stopped:
            await pending
        assert stopped.value.code == "TRIAL_STOPPING"
        async with AsyncSession(_engine) as db:
            reservation = await db.get(BenchmarkHostReservation, store.host_id)
            assert reservation.budget["stage"] == "planning"
    finally:
        resume.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_only_one_final_group_wins_concurrent_admission(planning, _engine):
    store, owner, _, _, _ = planning
    other = replace(ENVIRONMENT, resource_id="other")
    results = await asyncio.gather(
        admit(store, owner, (WORKER, ENVIRONMENT), stage="full"),
        admit(store, owner, (WORKER, other), stage="full"),
        return_exceptions=True,
    )
    assert len([value for value in results if isinstance(value, dict)]) == 1
    error = next(value for value in results if isinstance(value, Exception))
    assert isinstance(error, ProtocolError) and error.code == "ADMISSION_CONFLICT"
    async with AsyncSession(_engine) as db:
        saved = await db.scalar(select(BenchmarkHostReservation))
        assert saved.budget == next(value for value in results if isinstance(value, dict))


async def test_planning_budget_cannot_execute_native_commands(planning):
    _, _, scope, _, lease = planning
    authority = await install(planning)
    with pytest.raises(ProtocolError) as denied:
        async with authority.operation(request(scope, "exec", handle=lease.handle)):
            pytest.fail("Planning grants must not authorize native exec")
    assert denied.value.code == "PLANNING_WORKER_ONLY"
