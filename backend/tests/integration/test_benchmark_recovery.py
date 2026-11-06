"""Durable cleanup dependency and unavailable-provider behavior."""

import uuid
from dataclasses import replace

import pytest
from app.benchmarks.recovery import RecoveryService
from app.benchmarks.resource_authority import install_session
from app.models.benchmark_trial import BenchmarkResourceLease
from app.runtime.broker import ResourceLease
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.managed_spec import ManagedNetwork, NetworkRequest
from app.runtime.reconcile import RootlessReconciler
from evalhive_benchmark import ProtocolError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.test_benchmark_trials import (
    MANIFEST,
    PROVIDER,
    FakeRuntime,
    fixture,  # noqa: F401
    request,
)


async def test_unavailable_provider_does_not_release_reservation_or_leases(fixture):  # noqa: F811
    store, owner, _, actor, _, _, _ = fixture
    await store.transition(owner, "planning", "cleaning")
    recovery = RecoveryService(store, actor, {})
    result = await recovery.sweep(owner, "release")
    assert result[0]["error"]["code"] == "PROVIDER_UNAVAILABLE"
    with pytest.raises(ProtocolError, match="remain unreconciled"):
        await store.transition(owner, "cleaning", "finished")
    runtime = FakeRuntime()
    recovery.providers[PROVIDER] = lambda authority: RootlessReconciler(
        runtime, authority, PROVIDER
    )
    assert (await recovery.sweep(owner, "release"))[0]["state"] == "released"
    assert await recovery.sweep(owner, "release") == []
    await store.transition(owner, "cleaning", "finished")


async def test_unconfirmed_child_release_blocks_its_network_dependency(fixture, _engine):  # noqa: F811
    store, owner, scope, actor, original, authority, params = fixture
    await RootlessDockerProvider(FakeRuntime(), authority, MANIFEST).handle(
        request(scope, "release", handle=original.handle)
    )
    scope = replace(scope, session_id=str(uuid.uuid4()))
    network_id, environment_id = str(uuid.uuid4()), str(uuid.uuid4())
    network = ManagedNetwork(scope, network_id, "fixture-network")
    network_lease = ResourceLease(network_id, scope, "network", NetworkRequest(scope, network_id))
    environment = ResourceLease(
        environment_id,
        scope,
        "container",
        replace(original.intent, scope=scope, resource_id=environment_id, network=network),
    )
    await install_session(
        store,
        owner,
        scope,
        leases={"environment": (PROVIDER, environment), "network": (PROVIDER, network_lease)},
        actors=params["actors"],
    )
    await store.transition(owner, "planning", "cleaning")
    called = []

    class UnconfirmedProvider:
        async def handle(self, command):
            called.append(command["handle"])
            return {"handle": command["handle"], "state": "allocated"}

    recovery = RecoveryService(store, actor, {PROVIDER: lambda authority: UnconfirmedProvider()})
    results = await recovery.sweep(owner, "release")
    assert called == [environment_id]
    assert [value["error"]["code"] for value in results] == [
        "RECOVERY_INCOMPLETE",
        "DEPENDENCY_UNRESOLVED",
    ]
    async with AsyncSession(_engine) as db:
        assert (await db.get(BenchmarkResourceLease, uuid.UUID(network_id))).state == "planned"
