"""Scoped resource authority for regrade, without access to source execution leases."""

import json
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.benchmarks.broker import SqlBroker
from app.benchmarks.recovery import RecoveryService
from app.benchmarks.resource_authority import ActorPolicy, SqlResourceAuthority, install_session
from app.models.benchmark_evaluation_control import BenchmarkEvaluationControl
from app.models.benchmark_trial import (
    BenchmarkEnvironmentSession,
    BenchmarkOperation,
    BenchmarkResourceLease,
    BenchmarkTrial,
)
from app.runtime.admission import GroupPolicy, HostCapacity, ResourceDemand
from app.runtime.broker import ResourceLease
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.lease_codec import encode_lease
from app.runtime.managed_spec import ContainerSpec, OwnerScope
from evalhive_benchmark import ProtocolError
from evalhive_benchmark.peer import identity
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
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
from tests.integration.test_evaluation_controls import (
    controls as _controls_fixture,
)
from tests.integration.test_evaluation_controls import (
    driver as _driver_fixture,
)

controls, driver = _controls_fixture, _driver_fixture


@pytest_asyncio.fixture
async def resources(controls):
    controller, store, source, owner, _ = controls
    await store.start_planning(owner)

    async def measure():
        return HostCapacity(8192, 8192, 4, datetime.now(UTC).isoformat())

    await store.reserve(
        owner,
        demands=(ResourceDemand("environment", QUOTA, 256, 0.1, 10),),
        native_resources={},
        measure=measure,
        runtime_policy=POLICY,
        group_policy=GroupPolicy(),
    )
    scope = OwnerScope(str(WORKSPACE), str(source.trial_id), str(uuid.uuid4()), 1)
    actor, handle = uuid.uuid4(), str(uuid.uuid4())
    lease = ResourceLease(
        handle, scope, "container", ContainerSpec(scope, handle, IMAGE, quota=QUOTA)
    )
    policy = ActorPolicy(
        (PROVIDER,),
        ("environment",),
        ("planning", "cleaning"),
        ("allocate", "inspect", "stop", "release"),
    )
    await install_session(
        store,
        owner,
        scope,
        leases={"environment": (PROVIDER, lease)},
        actors={actor: policy},
        purpose="evaluation",
    )
    runtime = FakeRuntime()
    broker = SqlBroker(
        store,
        owner,
        actor,
        {PROVIDER: lambda authority: RootlessDockerProvider(runtime, authority, MANIFEST)},
    )
    # Retained execution rows are visible under the same source trial but must
    # never become a regrade route, even when actor/request identities match.
    old_scope = replace(scope, session_id=str(uuid.uuid4()))
    old_handle = str(uuid.uuid4())
    old_lease = ResourceLease(
        old_handle,
        old_scope,
        "container",
        ContainerSpec(old_scope, old_handle, IMAGE, quota=QUOTA),
        state="released",
    )
    frame, op_id = request(scope, request_id="shared"), uuid.uuid4()
    async with AsyncSession(store.engine) as db:
        db.add(
            BenchmarkEnvironmentSession(
                id=uuid.UUID(old_scope.session_id),
                trial_id=source.trial_id,
                workspace_id=WORKSPACE,
                origin_generation=1,
                purpose="execution",
                state="planned",
                plan_digest="f" * 64,
                private_plan=store.private.seal(
                    "session:" + old_scope.session_id,
                    json.loads(json.dumps({"actors": {str(actor): asdict(policy)}})),
                ),
            )
        )
        await db.flush()
        db.add(
            BenchmarkResourceLease(
                id=uuid.UUID(old_handle),
                session_id=uuid.UUID(old_scope.session_id),
                trial_id=source.trial_id,
                workspace_id=WORKSPACE,
                origin_generation=1,
                logical_id="environment",
                provider_id=PROVIDER,
                kind="container",
                state="released",
                private_value=store.private.seal("lease:" + old_handle, encode_lease(old_lease)),
            )
        )
        db.add(
            BenchmarkOperation(
                id=op_id,
                trial_id=source.trial_id,
                session_id=uuid.UUID(old_scope.session_id),
                generation=1,
                actor_id=actor,
                request_id="shared",
                content_hash=identity(frame),
                state="complete",
                private_request=store.private.seal("operation-request:" + str(op_id), frame),
                private_result=store.private.seal(
                    "operation-result:" + str(op_id), {"result": {"handle": old_handle}}
                ),
                finished_at=datetime.now(UTC),
            )
        )
        await db.commit()
    return store, source, owner, scope, actor, lease, old_scope, old_handle, broker, runtime


async def test_allocation_and_receipt_replay_are_scoped_to_evaluator(resources):
    store, source, owner, scope, _, lease, _, _, broker, runtime = resources
    async with AsyncSession(store.engine) as db:
        before = (await db.get(BenchmarkTrial, source.trial_id)).next_seq
    frame = request(scope, request_id="shared")
    result = await broker.handle(frame)
    assert result["handle"] == lease.handle and runtime.creates == 1
    assert await broker.replay(frame) == result and runtime.creates == 1
    async with AsyncSession(store.engine) as db:
        operations = (
            await db.scalars(
                select(BenchmarkOperation).where(BenchmarkOperation.request_id == "shared")
            )
        ).all()
        assert {op.control_id for op in operations} == {None, owner.control_id}
        assert (
            await db.get(BenchmarkEnvironmentSession, uuid.UUID(scope.session_id))
        ).control_id == owner.control_id
        assert (await db.get(BenchmarkTrial, source.trial_id)).next_seq == before


async def test_old_handle_and_direct_old_session_are_unavailable(resources):
    store, _, owner, scope, actor, _, old_scope, old_handle, broker, _ = resources
    with pytest.raises(ProtocolError) as route:
        await broker.handle(request(scope, "inspect", handle=old_handle))
    assert route.value.code == "UNREGISTERED_RESOURCE"
    authority = SqlResourceAuthority(store, owner, old_scope.session_id, actor, PROVIDER)
    with pytest.raises(ProtocolError) as direct:
        async with authority.operation(request(old_scope)):
            pytest.fail("Original execution session became evaluator authority")
    assert direct.value.code == "FOREIGN_SESSION"


async def test_recovery_uses_own_sessions_and_transferred_reservation(resources):
    store, source, owner, scope, actor, lease, _, old_handle, broker, _ = resources
    await broker.handle(request(scope))
    async with store.engine.begin() as db:
        await db.execute(
            update(BenchmarkEvaluationControl)
            .where(BenchmarkEvaluationControl.id == owner.control_id)
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    claimed = await store.claim(WORKSPACE, source.trial_id, owner.control_id, uuid.uuid4())

    class Reconciler:
        def __init__(self, authority):
            self.authority = authority

        async def handle(self, frame):
            async with self.authority.operation(frame) as call:
                selected = await call.resource(frame["handle"])
                assert selected.handle == lease.handle
                await call.save(replace(selected, state="released"))
                result = {"handle": selected.handle, "state": "released"}
                await call.complete(result)
                return result

    result = await RecoveryService(store, actor, {PROVIDER: Reconciler}).sweep(claimed, "release")
    assert len(result) == 1 and result[0]["state"] == "released"
    async with AsyncSession(store.engine) as db:
        assert (await db.get(BenchmarkResourceLease, uuid.UUID(old_handle))).state == "released"
        assert (await db.get(BenchmarkTrial, source.trial_id)).phase == "finished"


async def test_session_context_cannot_be_rebound(resources):
    store, _, _, scope, _, _, _, _, _, _ = resources
    async with store.engine.begin() as db:
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(
                update(BenchmarkEnvironmentSession)
                .where(BenchmarkEnvironmentSession.id == uuid.UUID(scope.session_id))
                .values(control_id=None)
            )
