"""Durable cleanup dependency and unavailable-provider behavior."""

import uuid
from dataclasses import asdict, replace
from unittest.mock import MagicMock

import pytest
from app.benchmarks.recovery import RecoveryService
from app.benchmarks.resource_authority import install_session
from app.models.benchmark_trial import BenchmarkResourceLease
from app.runtime.broker import ExportGrant, ResourceLease
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.managed_spec import ManagedNetwork, ManagedRuntimeError, NetworkRequest
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


async def test_known_native_rejection_can_finish_cleanup_and_release_host(fixture, _engine):  # noqa: F811
    store, owner, scope, actor, lease, authority, _ = fixture
    runtime = FakeRuntime()
    runtime.create = MagicMock(side_effect=ManagedRuntimeError("Native validation rejected"))
    with pytest.raises(ProtocolError) as rejected:
        await RootlessDockerProvider(runtime, authority, MANIFEST).handle(request(scope))
    assert rejected.value.code == "RESOURCE_REJECTED"
    async with AsyncSession(_engine) as db:
        assert (await db.get(BenchmarkResourceLease, uuid.UUID(lease.handle))).state == "rejected"
    await store.transition(owner, "planning", "cleaning")
    recovery = RecoveryService(
        store, actor, {PROVIDER: lambda value: RootlessReconciler(runtime, value, PROVIDER)}
    )
    assert (await recovery.sweep(owner, "release"))[0]["state"] == "released"
    await store.transition(owner, "cleaning", "finished")
    runtime.create.assert_called_once()


async def test_retired_resource_receipt_lookup_never_dispatches_missing_operations(fixture):  # noqa: F811
    _, _, scope, _, _, authority, _ = fixture
    runtime = FakeRuntime()
    provider = RootlessDockerProvider(runtime, authority, MANIFEST)
    original = request(scope)
    result = await provider.handle(original)
    assert await provider.replay(original) == result and runtime.creates == 1
    with pytest.raises(ProtocolError) as missing:
        await provider.replay(request(scope))
    assert missing.value.code == "RECEIPT_UNAVAILABLE" and runtime.creates == 1


@pytest.mark.parametrize(
    "required,code,complete",
    [
        (False, "ARTIFACT_MISSING", True),
        (True, "ARTIFACT_MISSING", False),
        (False, "PROVIDER_UNAVAILABLE", False),
    ],
)
async def test_recovery_skips_only_definitively_absent_optional_outputs(
    fixture,  # noqa: F811
    required,
    code,
    complete,
):
    store, owner, scope, actor, original, authority, params = fixture
    await RootlessDockerProvider(FakeRuntime(), authority, MANIFEST).handle(
        request(scope, "release", handle=original.handle)
    )
    scope = replace(scope, session_id=str(uuid.uuid4()))
    identity = str(uuid.uuid4())
    lease = replace(
        original,
        handle=identity,
        scope=scope,
        intent=replace(original.intent, scope=scope, resource_id=identity),
    )
    await install_session(
        store,
        owner,
        scope,
        leases={"environment": (PROVIDER, lease)},
        actors=params["actors"],
        outputs={
            "environment": {
                "result": {
                    "actors": [str(actor)],
                    "grant": asdict(ExportGrant("result", "/result", "private", 1024)),
                }
            }
        },
        registration={
            "output_plan": [{"id": "result", "resource_id": "environment", "required": required}]
        },
    )
    await store.transition(owner, "planning", "cleaning")

    class MissingExport:
        async def handle(self, command):
            assert command["operation"] == "export" and command["handle"] == identity
            raise ProtocolError(code, "Fixture output is unavailable", "recording")

    recovery = RecoveryService(store, actor, {})
    result = await recovery.capture_files(owner, {PROVIDER: lambda authority: MissingExport()})
    assert result["complete"] is complete
    assert result["native_state"] == "unavailable" and result["artifacts"] == []
    assert bool(result["absent_optional_outputs"]) is complete
    assert bool(result["errors"]) is not complete
