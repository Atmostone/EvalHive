"""External cleanup proofs survive restart and cannot bypass ownership fences."""

import copy
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.runtime.broker import ResourceLease
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.lease_codec import decode_lease, encode_lease
from app.runtime.managed_spec import (
    ContainerSpec,
    FinalizerSpec,
    ManagedContainer,
    ManagedRuntimeError,
    OwnerScope,
)
from app.runtime.reconcile import RootlessReconciler
from evalhive_benchmark import ProtocolError

from tests.runtime.authority_fixture import manifest


def resource():
    scope = OwnerScope(*(str(uuid.uuid4()) for _ in range(3)), 1)
    handle = str(uuid.uuid4())
    image = "sha256:" + "a" * 64
    spec = ContainerSpec(
        scope, handle, image, finalizer=FinalizerSpec(("/installed/cleanup",), 30, True)
    )
    return ResourceLease(
        handle,
        scope,
        "container",
        spec,
        ManagedContainer(scope, handle, "owned-container", image),
        "allocated",
    )


class Runtime:
    def __init__(self):
        self.client = SimpleNamespace(api=SimpleNamespace())
        self.state, self.stops = {"status": "running"}, []

    def inspect(self, native):
        return self.state

    def stop(self, native, *, remove):
        self.stops.append(remove)
        self.state = None if remove else {"status": "exited"}


class Call:
    def __init__(self, scope, *, fenced=False):
        self.scope, self.fenced, self.saved = scope, fenced, []

    async def save(self, lease):
        self.saved.append(lease)

    @asynccontextmanager
    async def observation(self):
        yield
        if self.fenced:
            raise ProtocolError("STALE_GENERATION", "Fixture ownership expired", "configuration")


def test_finalizer_codec_preserves_proof_and_reads_pre_feature_leases():
    lease = resource()
    assert decode_lease(encode_lease(lease)) == lease
    finished = replace(lease, finalized=True, state="released")
    assert decode_lease(encode_lease(finished)) == finished
    with pytest.raises(ValueError, match="proof"):
        replace(lease, state="released")
    legacy = replace(lease, intent=replace(lease.intent, finalizer=None))
    encoded = copy.deepcopy(encode_lease(legacy))
    del encoded["finalized"], encoded["intent"]["finalizer"]
    assert decode_lease(encoded) == legacy


def test_finalizer_requires_an_explicit_bounded_replay_safe_contract():
    for timeout, safe in [(30, False), (float("inf"), True), (601, True)]:
        with pytest.raises(ManagedRuntimeError, match="replay-safe"):
            FinalizerSpec(("/installed/cleanup",), timeout, safe)


async def test_successful_finalizer_is_saved_before_removal_and_not_repeated():
    lease, runtime = resource(), Runtime()
    call = Call(lease.scope)
    provider = RootlessDockerProvider(runtime, object(), manifest())
    provider._exec = AsyncMock(return_value={"exit_code": 0})
    finalized = await provider.finalize_resource(call, lease)
    assert finalized.finalized and call.saved == [finalized]
    assert not runtime.stops
    await provider._stop(call, finalized, release=True)
    assert provider._exec.await_count == 1 and runtime.stops == [True]
    assert call.saved[-1].state == "released" and call.saved[-1].finalized


async def test_failed_finalizer_quiesces_but_does_not_confirm_or_remove_resource():
    lease, runtime = resource(), Runtime()
    call = Call(lease.scope)
    provider = RootlessDockerProvider(runtime, object(), manifest())
    provider._exec = AsyncMock(return_value={"exit_code": 1})
    with pytest.raises(ProtocolError) as failure:
        await provider.finalize_resource(call, lease)
    assert failure.value.code == "RESOURCE_FINALIZATION_FAILED"
    assert runtime.stops == [False]
    assert call.saved[-1].state == "unsafe" and not call.saved[-1].finalized
    with pytest.raises(ProtocolError) as unavailable:
        await provider.finalize_resource(call, call.saved[-1])
    assert unavailable.value.code == "RESOURCE_FINALIZATION_UNAVAILABLE"
    assert provider._exec.await_count == 1


async def test_lost_owner_cannot_stop_a_successors_resource_after_callback_failure():
    lease, runtime = resource(), Runtime()
    call = Call(lease.scope, fenced=True)
    provider = RootlessDockerProvider(runtime, object(), manifest())
    provider._exec = AsyncMock(
        side_effect=ProtocolError("STALE_GENERATION", "Expired", "configuration")
    )
    with pytest.raises(ProtocolError) as failure:
        await provider.finalize_resource(call, lease)
    assert failure.value.code == "STALE_GENERATION"
    assert not runtime.stops and not call.saved


async def test_recovery_runs_the_same_finalizer_before_confirming_release():
    lease, runtime = resource(), Runtime()
    call = Call(lease.scope)
    provider = RootlessDockerProvider(runtime, object(), manifest())
    provider._exec = AsyncMock(return_value={"exit_code": 0})
    reconciler = RootlessReconciler(
        runtime, object(), manifest()["id"], finalizer=provider.finalize_resource
    )
    result = await reconciler._reconcile(call, lease, "release")
    assert result["state"] == "released"
    assert runtime.stops == [True]
    assert all(row.finalized for row in call.saved)


async def test_missing_container_does_not_prove_external_cleanup():
    lease, runtime = resource(), Runtime()
    runtime.state = None
    call = Call(lease.scope)
    reconciler = RootlessReconciler(runtime, object(), manifest()["id"])
    with pytest.raises(ProtocolError) as failure:
        await reconciler._reconcile(call, lease, "inspect")
    assert failure.value.code == "RESOURCE_FINALIZATION_UNAVAILABLE"
    assert not call.saved
