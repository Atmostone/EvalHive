"""Real rootless finalizers retain a durable proof across controller takeover."""

import asyncio
import io
import json
import os
import tarfile
import uuid

import pytest
from app.models.benchmark_trial import BenchmarkResourceLease
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.lease_codec import decode_lease
from app.runtime.reconcile import RootlessReconciler
from evalhive_benchmark import ProtocolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from tests.integration.test_benchmark_recovery_native import PROVIDER, request, takeover
from tests.integration.test_benchmark_recovery_native import native_trial as native_trial
from tests.runtime.authority_fixture import manifest

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_RECOVERY") != "1",
    reason="Requires an isolated native SQL/rootless fixture",
)


def recovery(runtime, authority):
    async def finalize(call, lease):
        provider = RootlessDockerProvider(runtime, authority, manifest())
        try:
            return await provider.finalize_resource(call, lease)
        finally:
            await provider.close()

    return RootlessReconciler(runtime, authority, PROVIDER, finalizer=finalize)


def allocation(scope, handle):
    command = request(scope, handle, "allocate", reconcile=False)
    del command["handle"]
    return command


@pytest.mark.parametrize("native_trial", ["finalizer"], indirect=True)
async def test_cold_owner_executes_cleanup_once_and_preserves_proof_on_release(
    native_trial, _engine
):
    store, owner, scope, actor, lease, authority, runtime = native_trial
    provider = RootlessDockerProvider(runtime, authority, manifest())
    await provider.handle(allocation(scope, lease.handle))
    native = await asyncio.to_thread(runtime.find, scope, lease.handle)
    await store.transition(owner, "planning", "cleaning")
    recovered, new_authority = await takeover(store, owner, actor, scope, _engine)
    with pytest.raises(ProtocolError):
        await provider.handle({**request(scope, lease.handle, "stop"), "payload": {}})
    assert (await asyncio.to_thread(runtime.inspect, native))["status"] == "running"
    stopped = await recovery(runtime, new_authority).handle(request(scope, lease.handle, "stop"))
    assert stopped["state"] == "stopped"
    chunks, _ = runtime.client.containers.get(native.container_id).get_archive("/tmp/finalized.txt")
    with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as archive:
        lines = archive.extractfile("finalized.txt").read().decode().splitlines()
    assert len(lines) == 1
    bound = json.loads(lines[0])
    assert bound["trial_id"] == scope.trial_id and bound["generation"] == scope.generation
    released = await recovery(runtime, new_authority).handle(
        request(scope, lease.handle, "release")
    )
    assert released["state"] == "released"
    async with AsyncSession(_engine) as db:
        row = await db.scalar(
            select(BenchmarkResourceLease)
            .where(BenchmarkResourceLease.id == uuid.UUID(lease.handle))
            .options(undefer(BenchmarkResourceLease.private_value))
        )
        saved = decode_lease(store.private.open("lease:" + lease.handle, row.private_value))
        assert saved.finalized and saved.state == "released"
    await provider.close()


@pytest.mark.parametrize("native_trial", ["finalizer-failure"], indirect=True)
async def test_failed_native_cleanup_stops_but_never_confirms_resource_release(native_trial):
    store, owner, scope, _, lease, authority, runtime = native_trial
    provider = RootlessDockerProvider(runtime, authority, manifest())
    await provider.handle(allocation(scope, lease.handle))
    await store.transition(owner, "planning", "cleaning")
    reconciler = recovery(runtime, authority)
    with pytest.raises(ProtocolError) as failed:
        await reconciler.handle(request(scope, lease.handle, "release"))
    assert failed.value.code == "RESOURCE_FINALIZATION_FAILED"
    native = await asyncio.to_thread(runtime.find, scope, lease.handle)
    assert native is not None
    assert (await asyncio.to_thread(runtime.inspect, native))["status"] in {"exited", "dead"}
    with pytest.raises(ProtocolError) as unavailable:
        await reconciler.handle(request(scope, lease.handle, "release"))
    assert unavailable.value.code == "RESOURCE_FINALIZATION_UNAVAILABLE"
    with pytest.raises(ProtocolError, match="remain unreconciled"):
        await store.transition(owner, "cleaning", "finished")
    await provider.close()
