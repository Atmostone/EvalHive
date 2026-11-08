"""PostgreSQL grants cleanup after expiry only from a frozen installed command."""

import asyncio
import uuid
from dataclasses import replace

import pytest
from app.benchmarks.resource_authority import SqlResourceAuthority, install_session
from app.runtime.broker import ResourceLease
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.finalizers import finalizer_payload
from app.runtime.managed_spec import FinalizerSpec
from evalhive_benchmark import ProtocolError

from tests.integration.test_benchmark_trials import MANIFEST, PROVIDER, FakeRuntime, request
from tests.integration.test_benchmark_trials import fixture as fixture


async def installed(fixture):
    store, owner, scope, actor, original, authority, params = fixture
    await RootlessDockerProvider(FakeRuntime(), authority, MANIFEST).handle(
        request(scope, "release", handle=original.handle)
    )
    scope = replace(scope, session_id=str(uuid.uuid4()))
    handle = str(uuid.uuid4())
    lease = ResourceLease(
        handle,
        scope,
        "container",
        replace(
            original.intent,
            scope=scope,
            resource_id=handle,
            finalizer=FinalizerSpec(("/installed/cleanup",), 30, True),
        ),
    )
    await install_session(
        store, owner, scope, leases={"environment": (PROVIDER, lease)}, actors=params["actors"]
    )
    authority = SqlResourceAuthority(store, owner, scope.session_id, actor, PROVIDER)
    await RootlessDockerProvider(FakeRuntime(), authority, MANIFEST).handle(request(scope))
    return store, owner, scope, lease, authority


async def test_finalizer_cannot_start_before_cleanup_or_change_the_registered_command(fixture):
    store, owner, scope, lease, authority = await installed(fixture)
    async with authority.operation(request(scope, "stop", handle=lease.handle)) as call:
        container = await call.resource(lease.handle)
        with pytest.raises(ProtocolError) as forbidden:
            await call.finalizer_execution(container, finalizer_payload(container))
        assert forbidden.value.code == "FINALIZER_FORBIDDEN"
        await call.complete({"checked": True})
    await store.transition(owner, "planning", "cleaning")
    async with authority.operation(request(scope, "stop", handle=lease.handle)) as call:
        container = await call.resource(lease.handle)
        changed = replace(
            container,
            intent=replace(
                container.intent, finalizer=FinalizerSpec(("/different/cleanup",), 30, True)
            ),
        )
        with pytest.raises(ProtocolError) as forbidden:
            await call.finalizer_execution(changed, finalizer_payload(changed))
        assert forbidden.value.code == "FINALIZER_FORBIDDEN"
        await call.complete({"checked": True})


@pytest.mark.parametrize("fixture", [2], indirect=True)
async def test_only_frozen_finalizer_receives_an_execution_grant_after_trial_expiry(fixture):
    store, owner, scope, lease, authority = await installed(fixture)
    await store.transition(owner, "planning", "cleaning")
    await asyncio.sleep(2.1)
    async with authority.operation(request(scope, "stop", handle=lease.handle)) as call:
        container = await call.resource(lease.handle)
        payload = finalizer_payload(container)
        with pytest.raises(ProtocolError) as forbidden_wait:
            async with call.observation():
                pass
        assert forbidden_wait.value.code == "INVALID_OBSERVATION"
        with pytest.raises(ProtocolError) as expired:
            await call.execution(container, payload)
        assert expired.value.code == "TRIAL_DEADLINE"
        grant = await call.finalizer_execution(container, payload)
        async with call.observation():
            pass
        assert grant.mode == "wait" and grant.timeout_seconds == 30
        assert grant.lease.intent.argv == ("/installed/cleanup",)
        assert grant.lease.intent.private_inputs == {}
        assert grant.lease.intent.environment == payload["environment"]
        await call.save(grant.lease)
        await call.complete({"granted": True})
