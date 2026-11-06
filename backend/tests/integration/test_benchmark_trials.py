"""Real PostgreSQL persistence, fencing and lifecycle checks; no external models."""

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from app.benchmarks.private_store import PrivateStore
from app.benchmarks.resource_authority import ActorPolicy, SqlResourceAuthority, install_session
from app.benchmarks.trials import TrialStore
from app.models.benchmark_trial import (
    BenchmarkEnvironmentSession,
    BenchmarkEventStream,
    BenchmarkHostReservation,
    BenchmarkJournalEntry,
    BenchmarkOperation,
    BenchmarkResourceLease,
    BenchmarkTrial,
)
from app.runtime.admission import GroupPolicy, HostCapacity, ResourceDemand
from app.runtime.broker import ResourceLease
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.lease_codec import decode_lease, encode_lease
from app.runtime.managed_spec import (
    ContainerSpec,
    ExecSpec,
    ManagedContainer,
    OwnerScope,
    PathMapping,
    Quota,
    RuntimePolicy,
)
from evalhive_benchmark import ProtocolError
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000002")
IMAGE = "sha256:" + "a" * 64
PROVIDER = "fixture.rootless"
POLICY = RuntimePolicy(PathMapping("/owned-host", "/owned-local"))
QUOTA = Quota(memory_mb=256, cpu_cores=0.1)
MANIFEST = {
    "protocol_version": "1.0",
    "kind": "environment_provider",
    "id": PROVIDER,
    "version": "1.0",
    "display_name": "Fixture",
    "entrypoint": ["fixture"],
    "image": "fixture",
    "config_schema": {"type": "object"},
    "capabilities": {
        name: "native" for name in ["allocate", "exec", "inspect", "stop", "export", "release"]
    },
    "requirements": {},
}


@pytest_asyncio.fixture
async def fixture(_engine, db_session):
    store = TrialStore(_engine, PrivateStore("isolated-benchmark-storage-fixture"))
    trial = await store.create(
        WORKSPACE,
        snapshot={"title": "Fixture case"},
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    owner = await store.claim(WORKSPACE, trial.id, uuid.uuid4())
    await store.transition(owner, "created", "planning")

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
    scope = OwnerScope(str(WORKSPACE), str(trial.id), str(uuid.uuid4()), owner.generation)
    handle, actor = str(uuid.uuid4()), uuid.uuid4()
    spec = ContainerSpec(
        scope, handle, IMAGE, quota=QUOTA, environment={"PRIVATE_VALUE": "fixture-private"}
    )
    lease = ResourceLease(handle, scope, "container", spec)
    policy = ActorPolicy(
        (PROVIDER,),
        ("environment",),
        ("planning", "preparing", "ready", "driving", "capturing", "evaluating", "cleaning"),
        ("allocate", "exec", "inspect", "stop", "export", "release"),
        ("fixture.private",),
    )
    params = {
        "leases": {"environment": (PROVIDER, lease)},
        "actors": {actor: policy},
        "private_inputs": {"fixture.private": b"private bytes"},
    }
    await install_session(store, owner, scope, **params)
    authority = SqlResourceAuthority(store, owner, scope.session_id, actor, PROVIDER)
    return store, owner, scope, actor, lease, authority, params


def request(scope, operation="allocate", *, request_id=None, handle=None, payload=None):
    value = {
        "protocol_version": "1.0",
        "type": "broker_request",
        "id": request_id or uuid.uuid4().hex,
        "scope": {
            "workspace_id": scope.workspace_id,
            "trial_id": scope.trial_id,
            "generation": scope.generation,
        },
        "provider": PROVIDER,
        "operation": operation,
        "payload": {"resource_id": "environment"}
        if payload is None and operation == "allocate"
        else payload or {},
    }
    if handle is not None:
        value["handle"] = handle
    return value


class FakeRuntime:
    def __init__(self):
        self.creates = 0
        self.client = SimpleNamespace(api=None)

    def create(self, spec):
        self.creates += 1
        return ManagedContainer(spec.scope, spec.resource_id, "owned-native", spec.image_id)

    def inspect(self, native):
        return {"status": "running"}

    def stop(self, native, *, remove=False):
        pass


async def advance_to_driving(store, owner):
    for expected, target in [
        ("planning", "preparing"),
        ("preparing", "ready"),
        ("ready", "driving"),
    ]:
        await store.transition(owner, expected, target)


async def test_resource_receipt_replays_after_new_authority_instance(fixture, _engine):
    store, owner, scope, actor, lease, authority, _ = fixture
    runtime = FakeRuntime()
    provider = RootlessDockerProvider(runtime, authority, MANIFEST)
    command = request(scope)
    result = await provider.handle(command)
    assert result["handle"] == lease.handle and runtime.creates == 1
    restarted = SqlResourceAuthority(
        TrialStore(_engine, PrivateStore("isolated-benchmark-storage-fixture")),
        owner,
        scope.session_id,
        actor,
        PROVIDER,
    )
    assert await RootlessDockerProvider(runtime, restarted, MANIFEST).handle(command) == result
    assert runtime.creates == 1
    changed = {**command, "payload": {"resource_id": "other"}}
    with pytest.raises(ProtocolError, match="different content"):
        await provider.handle(changed)


async def test_resource_private_state_never_enters_public_journal(fixture, _engine):
    store, _, scope, _, lease, authority, _ = fixture
    await RootlessDockerProvider(FakeRuntime(), authority, MANIFEST).handle(request(scope))
    async with AsyncSession(_engine) as db:
        row = await db.scalar(
            select(BenchmarkResourceLease)
            .where(BenchmarkResourceLease.id == uuid.UUID(lease.handle))
            .options(undefer(BenchmarkResourceLease.private_value))
        )
        assert b"fixture-private" not in row.private_value
        assert (
            decode_lease(store.private.open("lease:" + lease.handle, row.private_value)).intent
            == lease.intent
        )
        public = [
            entry.public_data for entry in (await db.scalars(select(BenchmarkJournalEntry))).all()
        ]
        assert "fixture-private" not in json.dumps(public)
        assert "owned-native" not in json.dumps(public)


async def test_uncertain_intent_is_not_reissued(fixture, _engine):
    store, _, scope, _, lease, authority, _ = fixture
    command = request(scope)
    with pytest.raises(ConnectionError):
        async with authority.operation(command) as call:
            value = await call.allocation("environment")
            await call.save(replace(value, state="creating"))
            raise ConnectionError("private native error")
    with pytest.raises(ProtocolError, match="requires reconciliation"):
        async with authority.operation(command):
            pytest.fail("Uncertain effect must never be reissued")
    async with AsyncSession(_engine) as db:
        assert (await db.get(BenchmarkResourceLease, uuid.UUID(lease.handle))).state == "creating"
        op = await db.scalar(select(BenchmarkOperation))
        assert op.state == "uncertain"
        assert "private native error" not in str(op.error_code)


async def test_cancel_during_resource_effect_records_uncertainty(fixture, _engine):
    _, _, scope, _, _, authority, _ = fixture
    command = request(scope)
    with pytest.raises(asyncio.CancelledError):
        async with authority.operation(command) as call:
            await call.save(replace(await call.allocation("environment"), state="creating"))
            raise asyncio.CancelledError
    async with AsyncSession(_engine) as db:
        assert (await db.scalar(select(BenchmarkOperation))).state == "uncertain"
    with pytest.raises(ProtocolError, match="requires reconciliation"):
        async with authority.operation(command):
            pytest.fail("Cancelled effect must never be reissued")


async def test_heartbeat_and_cancellation_remain_available_while_fence_is_held(fixture):
    store, owner, scope, _, _, authority, _ = fixture
    async with authority.operation(request(scope)) as call:
        await call.save(replace(await call.allocation("environment"), state="creating"))
        await asyncio.wait_for(store.heartbeat(owner), 2)
        assert await asyncio.wait_for(store.request_cancel(WORKSPACE, owner.trial_id), 2)
        await call.complete({"checked": True})
    with pytest.raises(ProtocolError, match="stopping"):
        async with authority.operation(request(scope)):
            pytest.fail("Cancellation must prevent new effects")


async def test_journal_remains_live_across_native_intent_commits(fixture, _engine):
    store, owner, scope, actor, _, authority, _ = fixture
    await store.register_stream(owner, actor)
    async with authority.operation(request(scope)) as call:
        lease = await call.allocation("environment")
        await call.save(replace(lease, state="creating"))
        # A native command may remain in flight for minutes. Event persistence
        # must not wait for its resource/ownership fence to be released.
        await asyncio.wait_for(store.ingest(owner, actor, [event(owner, 0)]), 2)
        await call.save(replace(lease, state="creating"))
        await asyncio.wait_for(store.ingest(owner, actor, [event(owner, 1)]), 2)
        await call.complete({"checked": True})
    async with AsyncSession(_engine) as db:
        trial = await db.get(BenchmarkTrial, owner.trial_id)
        entries = (
            await db.scalars(select(BenchmarkJournalEntry).order_by(BenchmarkJournalEntry.seq))
        ).all()
        assert [entry.seq for entry in entries] == list(range(trial.next_seq))


async def test_concurrent_event_streams_and_resource_receipts_have_one_contiguous_journal(
    fixture, _engine
):
    store, owner, scope, actor, lease, authority, _ = fixture
    other = uuid.uuid4()
    for selected in (actor, other):
        await store.register_stream(owner, selected)
    provider = RootlessDockerProvider(FakeRuntime(), authority, MANIFEST)
    await provider.handle(request(scope))

    async def events(selected):
        for sequence in range(10):
            await store.ingest(owner, selected, [event(owner, sequence)])

    async def inspect():
        for _ in range(10):
            await provider.handle(request(scope, "inspect", handle=lease.handle))

    await asyncio.gather(events(actor), events(other), inspect())
    async with AsyncSession(_engine) as db:
        trial = await db.get(BenchmarkTrial, owner.trial_id)
        entries = (
            await db.scalars(select(BenchmarkJournalEntry).order_by(BenchmarkJournalEntry.seq))
        ).all()
        assert [entry.seq for entry in entries] == list(range(trial.next_seq))
        assert len([entry for entry in entries if entry.source_seq is not None]) == 20


async def test_owner_takeover_waits_for_committed_native_fence(fixture, _engine):
    store, owner, scope, _, _, authority, _ = fixture
    async with authority.operation(request(scope)) as call:
        await call.save(replace(await call.allocation("environment"), state="creating"))
        async with _engine.begin() as conn:
            await conn.execute(
                update(BenchmarkTrial)
                .where(BenchmarkTrial.id == owner.trial_id)
                .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        takeover = asyncio.create_task(store.claim(WORKSPACE, owner.trial_id, uuid.uuid4()))
        await asyncio.sleep(0.1)
        assert not takeover.done()
        await call.complete({"effect": "confirmed"})
    new_owner = await asyncio.wait_for(takeover, 3)
    assert new_owner.generation == owner.generation + 1
    with pytest.raises(ProtocolError, match="no longer owns"):
        async with authority.operation(request(scope)):
            pytest.fail("Old controller must be fenced")


async def test_recovery_uses_new_controller_and_old_resource_origin(fixture, _engine):
    store, owner, scope, actor, lease, authority, _ = fixture
    runtime = FakeRuntime()
    await RootlessDockerProvider(runtime, authority, MANIFEST).handle(request(scope))
    async with _engine.begin() as conn:
        await conn.execute(
            update(BenchmarkTrial)
            .where(BenchmarkTrial.id == owner.trial_id)
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    recovered = await store.claim(WORKSPACE, owner.trial_id, uuid.uuid4())
    authority = SqlResourceAuthority(
        store, recovered, scope.session_id, actor, PROVIDER, recovery=True
    )
    provider = RootlessDockerProvider(runtime, authority, MANIFEST)
    assert (await provider.handle(request(scope, "inspect", handle=lease.handle)))[
        "state"
    ] == "allocated"
    with pytest.raises(ProtocolError, match="Recovery may only"):
        await provider.handle(request(scope))
    with pytest.raises(ProtocolError, match="remain unreconciled"):
        await store.transition(recovered, "cleaning", "finished")
    await provider.handle(request(scope, "release", handle=lease.handle))
    await store.transition(recovered, "cleaning", "finished")
    async with AsyncSession(_engine) as db:
        trial = await db.get(BenchmarkTrial, owner.trial_id)
        assert trial.execution_status == "lost" and trial.recording_status == "incomplete"
        assert trial.cleanup_status == "complete"
        assert await db.get(BenchmarkHostReservation, store.host_id) is None


def event(owner, seq, *, event_id=None, source="native"):
    return {
        "protocol_version": "1.0",
        "type": "event",
        "id": event_id or f"event-{seq}",
        "scope": {
            "workspace_id": str(owner.workspace_id),
            "trial_id": str(owner.trial_id),
            "generation": owner.generation,
        },
        "seq": seq,
        "source": source,
        "event": "native",
        "data": {"private": "private-native-observation"},
    }


async def test_contiguous_journal_repeats_conflicts_and_completion_barrier(fixture, _engine):
    store, owner, _, actor, _, _, _ = fixture
    await advance_to_driving(store, owner)
    await store.register_stream(owner, actor)
    with pytest.raises(ProtocolError, match="contiguous"):
        await store.ingest(owner, actor, [event(owner, 1)])
    assert await store.ingest(owner, actor, [event(owner, 0)]) == {"accepted_through": 0}
    assert await store.ingest(owner, actor, [event(owner, 0), event(owner, 1, source="other")]) == {
        "accepted_through": 1
    }
    with pytest.raises(ProtocolError, match="different content"):
        await store.ingest(owner, actor, [{**event(owner, 0), "data": {"changed": True}}])
    with pytest.raises(ProtocolError, match="all preceding"):
        await store.seal_stream(owner, actor, 2)
    with pytest.raises(ProtocolError, match="event barriers"):
        await store.finish_execution(owner, "success")
    await store.seal_stream(owner, actor, 1)
    await store.seal_stream(owner, actor, 1)
    await store.finish_execution(owner, "success")
    await store.transition(owner, "driving", "capturing")
    await store.transition(owner, "capturing", "evaluating")
    async with AsyncSession(_engine) as db:
        trial = await db.get(BenchmarkTrial, owner.trial_id)
        assert trial.execution_status == "success" and trial.evaluation_status == "running"
        assert trial.cleanup_status == "pending"
        assert (await db.scalar(select(BenchmarkResourceLease))).state == "planned"
        entries = (
            await db.scalars(select(BenchmarkJournalEntry).order_by(BenchmarkJournalEntry.seq))
        ).all()
        assert [entry.seq for entry in entries] == list(range(trial.next_seq))
        assert "private-native-observation" not in json.dumps(
            [entry.public_data for entry in entries]
        )
        assert (await db.scalar(select(BenchmarkEventStream))).final_seq == 1


async def test_event_batch_rolls_back_all_records_after_gap(fixture, _engine):
    store, owner, _, actor, _, _, _ = fixture
    await store.register_stream(owner, actor)
    with pytest.raises(ProtocolError, match="contiguous"):
        await store.ingest(owner, actor, [event(owner, 0), event(owner, 2)])
    async with AsyncSession(_engine) as db:
        assert (await db.scalar(select(BenchmarkEventStream))).next_seq == 0
    assert await store.ingest(owner, actor, [event(owner, 0)]) == {"accepted_through": 0}


async def test_phase_checkpoint_allows_later_capture_and_evaluation_events(fixture):
    store, owner, _, actor, _, _, _ = fixture
    await advance_to_driving(store, owner)
    await store.register_stream(owner, actor)
    await store.ingest(owner, actor, [event(owner, 0)])
    await store.checkpoint_stream(owner, actor, 0)
    await store.finish_execution(owner, "success")
    await store.transition(owner, "driving", "capturing")
    await store.ingest(owner, actor, [event(owner, 1, source="capture")])
    await store.checkpoint_stream(owner, actor, 1)
    await store.transition(owner, "capturing", "evaluating")
    await store.ingest(owner, actor, [event(owner, 2, source="evaluation")])
    await store.seal_stream(owner, actor, 2)
    with pytest.raises(ProtocolError, match="sealed"):
        await store.ingest(owner, actor, [event(owner, 3)])


async def test_expired_trial_keeps_host_reserved_until_reconciliation(fixture, _engine):
    store, owner, _, _, _, _, _ = fixture
    async with _engine.begin() as conn:
        await conn.execute(
            update(BenchmarkTrial)
            .where(BenchmarkTrial.id == owner.trial_id)
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    trial = await store.create(
        WORKSPACE, snapshot={}, deadline_at=datetime.now(UTC) + timedelta(minutes=1)
    )
    other = await store.claim(WORKSPACE, trial.id, uuid.uuid4())
    await store.transition(other, "created", "planning")

    async def measure():
        pytest.fail("A busy host must not reach fresh admission")

    with pytest.raises(ProtocolError, match="remains reserved"):
        await store.reserve(
            other,
            demands=(),
            native_resources={},
            measure=measure,
            runtime_policy=POLICY,
            group_policy=GroupPolicy(),
        )


async def test_database_rejects_rewriting_journal_and_frozen_inputs(fixture, _engine):
    _, owner, scope, _, _, _, _ = fixture
    for statement in [
        update(BenchmarkTrial)
        .where(BenchmarkTrial.id == owner.trial_id)
        .values(snapshot={"rewrite": True}),
        update(BenchmarkJournalEntry)
        .where(BenchmarkJournalEntry.trial_id == owner.trial_id)
        .values(public_data={"rewrite": True}),
        update(BenchmarkEnvironmentSession)
        .where(BenchmarkEnvironmentSession.id == uuid.UUID(scope.session_id))
        .values(plan_digest="0" * 64),
    ]:
        with pytest.raises(DBAPIError, match="immutable"):
            async with _engine.begin() as conn:
                await conn.execute(statement)


async def test_failed_resource_receipt_is_repeatable_without_new_effect(fixture, _engine):
    _, _, scope, _, _, authority, _ = fixture
    provider = RootlessDockerProvider(FakeRuntime(), authority, MANIFEST)
    command = request(scope, payload={"resource_id": "not-granted"})
    for _ in range(2):
        with pytest.raises(ProtocolError) as rejected:
            await provider.handle(command)
        assert rejected.value.code == "UNREGISTERED_RESOURCE"
    async with AsyncSession(_engine) as db:
        assert (await db.scalar(select(BenchmarkOperation))).state == "failed"


async def test_resource_plan_cannot_consume_same_reservation_twice(fixture):
    store, owner, scope, _, lease, _, params = fixture
    scope = replace(scope, session_id=str(uuid.uuid4()))
    handle = str(uuid.uuid4())
    duplicate = replace(
        lease,
        handle=handle,
        scope=scope,
        intent=replace(lease.intent, scope=scope, resource_id=handle),
    )
    with pytest.raises(ProtocolError, match="live lease"):
        await install_session(
            store,
            owner,
            scope,
            leases={"environment": (PROVIDER, duplicate)},
            actors=params["actors"],
        )


async def test_repeated_host_admission_cannot_change_the_reserved_budget(fixture):
    store, owner, _, _, _, _, _ = fixture

    async def measure():
        pytest.fail("An existing admission must not be measured or replaced")

    with pytest.raises(ProtocolError, match="different resource requirements"):
        await store.reserve(
            owner,
            demands=(ResourceDemand("environment", Quota(512, 0.1), 256, 0.1, 10),),
            native_resources={},
            measure=measure,
            runtime_policy=POLICY,
            group_policy=GroupPolicy(),
        )


async def test_unsealed_stream_marks_recording_incomplete_without_preventing_cleanup(
    fixture, _engine
):
    store, owner, scope, actor, lease, authority, _ = fixture
    await store.register_stream(owner, actor)
    await store.ingest(owner, actor, [event(owner, 0)])
    await store.transition(owner, "planning", "cleaning")
    provider = RootlessDockerProvider(FakeRuntime(), authority, MANIFEST)
    await provider.handle(request(scope, "release", handle=lease.handle))
    await store.transition(owner, "cleaning", "finished")
    async with AsyncSession(_engine) as db:
        trial = await db.get(BenchmarkTrial, owner.trial_id)
        assert trial.recording_status == "incomplete" and trial.cleanup_status == "complete"
    with pytest.raises(ProtocolError, match="cannot register"):
        await store.register_stream(owner, uuid.uuid4())


async def test_session_plan_is_immutable_and_enforces_admitted_quotas(fixture):
    store, owner, scope, actor, lease, _, params = fixture
    assert await install_session(store, owner, scope, **params) == uuid.UUID(scope.session_id)
    changed = {**params, "private_inputs": {"fixture.private": b"changed"}}
    with pytest.raises(ProtocolError, match="immutable"):
        await install_session(store, owner, scope, **changed)
    new_scope = replace(scope, session_id=str(uuid.uuid4()))
    new_handle = str(uuid.uuid4())
    excessive = ResourceLease(
        new_handle,
        new_scope,
        "container",
        replace(
            lease.intent,
            scope=new_scope,
            resource_id=new_handle,
            quota=Quota(memory_mb=512, cpu_cores=0.1),
        ),
    )
    with pytest.raises(ProtocolError, match="host reservation"):
        await install_session(
            store,
            owner,
            new_scope,
            leases={"environment": (PROVIDER, excessive)},
            actors=params["actors"],
        )
    forbidden = SqlResourceAuthority(store, owner, scope.session_id, uuid.uuid4(), PROVIDER)
    with pytest.raises(ProtocolError, match="lacks permission"):
        async with forbidden.operation(request(scope)):
            pytest.fail("Unknown actor cannot allocate")


async def test_exec_private_key_and_inputs_survive_database_reconstruction(fixture, _engine):
    store, _, scope, _, container, authority, _ = fixture
    provider = RootlessDockerProvider(FakeRuntime(), authority, MANIFEST)
    await provider.handle(request(scope))
    command = request(
        scope,
        "exec",
        handle=container.handle,
        payload={"argv": ["echo", "fixture"], "private_input_refs": {"DATA": "fixture.private"}},
    )
    async with authority.operation(command) as call:
        grant = await call.execution(await call.resource(container.handle), command["payload"])
        lease = replace(grant.lease, state="creating")
        await call.save(lease)
        await call.complete({"prepared": True})
    async with AsyncSession(_engine) as db:
        row = await db.scalar(
            select(BenchmarkResourceLease)
            .where(BenchmarkResourceLease.id == uuid.UUID(lease.handle))
            .options(undefer(BenchmarkResourceLease.private_value))
        )
        restored = decode_lease(store.private.open("lease:" + lease.handle, row.private_value))
        assert restored == lease
        assert restored.intent.private_inputs == {"DATA": b"private bytes"}
        assert len(restored.intent.control_key) == 32
        assert isinstance(restored.intent, ExecSpec)
        assert encode_lease(restored) == encode_lease(lease)


@pytest.mark.parametrize(
    "phase", ["created", "planning", "preparing", "ready", "driving", "capturing", "evaluating"]
)
async def test_cancellation_in_each_phase_preserves_separate_cleanup(fixture, phase, _engine):
    store, owner, _, _, _, _, _ = fixture
    if phase == "created":
        # The fixture has already passed planning; use an independent trial.
        trial = await store.create(
            WORKSPACE, snapshot={}, deadline_at=datetime.now(UTC) + timedelta(minutes=1)
        )
        owner = await store.claim(WORKSPACE, trial.id, uuid.uuid4())
    else:
        order = ["planning", "preparing", "ready", "driving", "capturing", "evaluating"]
        for previous, target in zip(order, order[1:], strict=False):
            if previous == phase:
                break
            if target == "evaluating":
                await store.finish_execution(owner, "success")
            await store.transition(owner, previous, target)
    assert await store.request_cancel(WORKSPACE, owner.trial_id)
    await store.transition(owner, phase, "cleaning")
    async with AsyncSession(_engine) as db:
        trial = await db.get(BenchmarkTrial, owner.trial_id)
        assert trial.phase == "cleaning" and trial.cleanup_status == "running"
        assert trial.evaluation_status != "success"


async def test_scope_and_expired_controller_reject_writes(fixture, _engine):
    store, owner, _, actor, _, _, _ = fixture
    with pytest.raises(ProtocolError, match="workspace"):
        await store.claim(uuid.uuid4(), owner.trial_id, uuid.uuid4())
    assert not await store.request_cancel(uuid.uuid4(), owner.trial_id)
    await store.register_stream(owner, actor)
    async with _engine.begin() as conn:
        await conn.execute(
            update(BenchmarkTrial)
            .where(BenchmarkTrial.id == owner.trial_id)
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    with pytest.raises(ProtocolError, match="expired"):
        await store.ingest(owner, actor, [event(owner, 0)])


def test_authenticated_storage_rejects_tampering_wrong_row_and_wrong_key():
    store = PrivateStore("fixture-storage-key")
    encrypted = store.seal("lease:one", {"secret": "private"})
    assert store.open("lease:one", encrypted) == {"secret": "private"}
    for other, context, value in [
        (store, "lease:two", encrypted),
        (PrivateStore("other-key"), "lease:one", encrypted),
        (store, "lease:one", encrypted[:-1] + bytes([encrypted[-1] ^ 1])),
    ]:
        with pytest.raises(ValueError, match="authenticated"):
            other.open(context, value)
