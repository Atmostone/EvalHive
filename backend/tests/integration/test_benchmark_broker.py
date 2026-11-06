"""Broker dispatch remains scoped and idempotent across multiple native sessions."""

import asyncio
import uuid
from dataclasses import replace

import pytest
import pytest_asyncio
from app.benchmarks.broker import SqlBroker
from app.benchmarks.recovery import recovery_order
from app.benchmarks.resource_authority import ActorPolicy, SqlResourceAuthority, install_session
from app.models.benchmark_trial import BenchmarkOperation
from app.runtime.broker import ResourceLease
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.lease_codec import encode_lease
from app.runtime.managed_spec import ManagedNetwork, NetworkRequest
from evalhive_benchmark import ProtocolError
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.test_benchmark_planning_admission import (
    ENVIRONMENT,
    WORKER,
    admit,
    install,
    planning,  # noqa: F401
)
from tests.integration.test_benchmark_trials import MANIFEST, PROVIDER, FakeRuntime, request


@pytest_asyncio.fixture
async def routed(planning):  # noqa: F811
    store, owner, scope, actor, lease = planning
    await install(planning)
    await admit(store, owner, (WORKER, ENVIRONMENT), stage="full")
    second_scope = replace(scope, session_id=str(uuid.uuid4()))
    second_id = str(uuid.uuid4())
    second = ResourceLease(
        second_id,
        second_scope,
        "container",
        replace(lease.intent, scope=second_scope, resource_id=second_id, role="environment"),
    )
    await install_session(
        store,
        owner,
        second_scope,
        leases={"environment": (PROVIDER, second)},
        actors={
            actor: ActorPolicy(
                (PROVIDER,),
                ("environment",),
                ("planning", "cleaning"),
                ("allocate", "inspect", "release"),
            )
        },
    )
    runtime = FakeRuntime()
    broker = SqlBroker(
        store,
        owner,
        actor,
        {PROVIDER: lambda authority: RootlessDockerProvider(runtime, authority, MANIFEST)},
    )
    return broker, runtime, scope, second_scope


async def test_dispatch_across_sessions_and_pure_replay_after_release(routed):
    broker, runtime, scope, _ = routed
    allocate = request(scope, payload={"resource_id": "worker"})
    first = await broker.handle(allocate)
    await broker.handle(request(scope, "release", handle=first["handle"]))
    assert await broker.replay(allocate) == first
    assert await broker.handle(allocate) == first
    assert runtime.creates == 1
    second = await broker.handle(request(scope, payload={"resource_id": "environment"}))
    assert second["handle"] != first["handle"] and runtime.creates == 2


async def test_same_id_cannot_cross_resource_sessions_under_concurrent_dispatch(routed, _engine):
    broker, runtime, scope, _ = routed
    results = await asyncio.gather(
        broker.handle(request(scope, request_id="shared-id", payload={"resource_id": "worker"})),
        broker.handle(
            request(scope, request_id="shared-id", payload={"resource_id": "environment"})
        ),
        return_exceptions=True,
    )
    assert runtime.creates == 1
    error = next(value for value in results if isinstance(value, Exception))
    assert isinstance(error, ProtocolError) and error.code == "COMMAND_CONFLICT"
    async with AsyncSession(_engine) as db:
        assert len((await db.scalars(select(BenchmarkOperation))).all()) == 1


async def test_direct_authority_cannot_bypass_global_request_identity(routed):
    broker, runtime, scope, other = routed
    await broker.handle(request(scope, request_id="shared-id", payload={"resource_id": "worker"}))
    authority = SqlResourceAuthority(
        broker.store, broker.owner, other.session_id, broker.actor_id, PROVIDER
    )
    with pytest.raises(ProtocolError) as conflict:
        await RootlessDockerProvider(runtime, authority, MANIFEST).handle(
            request(scope, request_id="shared-id", payload={"resource_id": "environment"})
        )
    assert conflict.value.code == "COMMAND_CONFLICT" and runtime.creates == 1


async def test_missing_durable_receipt_never_dispatches_allocation(routed):
    broker, runtime, scope, _ = routed
    with pytest.raises(ProtocolError) as missing:
        await broker.replay(request(scope, payload={"resource_id": "worker"}))
    assert missing.value.code == "RECEIPT_UNAVAILABLE" and runtime.creates == 0


async def test_actor_and_workspace_cannot_be_selected_by_the_request(routed):
    broker, runtime, scope, _ = routed
    foreign = replace(scope, workspace_id=str(uuid.uuid4()))
    with pytest.raises(ProtocolError) as rejected:
        await broker.handle(request(foreign, payload={"resource_id": "worker"}))
    assert rejected.value.code == "STALE_GENERATION"
    another = SqlBroker(broker.store, broker.owner, uuid.uuid4(), broker.factories)
    with pytest.raises(ProtocolError) as forbidden:
        await another.handle(request(scope, payload={"resource_id": "worker"}))
    assert forbidden.value.code == "ACTOR_FORBIDDEN" and runtime.creates == 0


async def test_removed_provider_is_explicit_and_does_not_fall_back(routed):
    broker, runtime, scope, _ = routed
    unavailable = SqlBroker(broker.store, broker.owner, broker.actor_id, {})
    with pytest.raises(ProtocolError) as missing:
        await unavailable.handle(request(scope, payload={"resource_id": "worker"}))
    assert missing.value.code == "PROVIDER_UNAVAILABLE" and runtime.creates == 0


@pytest.mark.parametrize(
    "field,value", [("content_hash", "0" * 64), ("private_result", b"changed")]
)
async def test_original_broker_route_and_receipt_are_immutable_in_postgres(
    routed, _engine, field, value
):
    broker, _, scope, _ = routed
    await broker.handle(request(scope, payload={"resource_id": "worker"}))
    async with AsyncSession(_engine) as db:
        with pytest.raises(DBAPIError):
            await db.execute(update(BenchmarkOperation).values(**{field: value}))


async def test_failed_broker_outcome_cannot_be_rewritten(routed, _engine):
    broker, runtime, scope, _ = routed
    with pytest.raises(ProtocolError):
        await broker.handle(request(scope, payload={"resource_id": "worker", "unsupported": True}))
    assert runtime.creates == 0
    async with AsyncSession(_engine) as db:
        stored = await db.scalar(select(BenchmarkOperation))
        assert stored.state == "failed"
        with pytest.raises(DBAPIError):
            await db.execute(update(BenchmarkOperation).values(error_code="CHANGED"))


async def test_declared_dependencies_are_enforced_and_cannot_be_dropped(planning):  # noqa: F811
    store, owner, scope, actor, lease = planning
    await admit(store, owner, (WORKER,), stage="full")
    network_id = str(uuid.uuid4())
    network = ResourceLease(network_id, scope, "network", NetworkRequest(scope, network_id))
    dependent = replace(lease, dependencies=(network_id,))
    await install_session(
        store,
        owner,
        scope,
        leases={"worker": (PROVIDER, dependent), "network": (PROVIDER, network)},
        actors={
            actor: ActorPolicy(
                (PROVIDER,), ("worker", "network"), ("planning",), ("allocate", "inspect")
            )
        },
    )
    authority = SqlResourceAuthority(store, owner, scope.session_id, actor, PROVIDER)

    class Runtime(FakeRuntime):
        def create_network(self, scope, resource_id, **kwargs):
            return ManagedNetwork(scope, resource_id, "fixture-network")

    runtime = Runtime()
    provider = RootlessDockerProvider(runtime, authority, MANIFEST)
    with pytest.raises(ProtocolError) as blocked:
        await provider.handle(request(scope, payload={"resource_id": "worker"}))
    assert blocked.value.code == "RESOURCE_NOT_READY" and runtime.creates == 0
    await provider.handle(request(scope, payload={"resource_id": "network"}))
    async with authority.operation(request(scope, payload={"resource_id": "worker"})) as call:
        granted = await call.allocation("worker")
        with pytest.raises(ProtocolError) as dropping:
            await call.save(replace(granted, dependencies=()))
        assert dropping.value.code == "INVALID_INTENT"
        await call.complete({"dependency_guard": True})
    await provider.handle(request(scope, payload={"resource_id": "worker"}))
    assert runtime.creates == 1
    order, _ = recovery_order(
        {dependent.handle: set(encode_lease(dependent)["dependencies"]), network_id: set()}
    )
    assert order == [dependent.handle, network_id]
