"""Opt-in PostgreSQL + real rootless recovery, with no external model requests."""

import asyncio
import io
import os
import tarfile
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import docker
import pytest
import pytest_asyncio
from app.benchmarks.deadlines import DeadlineService
from app.benchmarks.private_store import PrivateStore
from app.benchmarks.recovery import RecoveryService
from app.benchmarks.resource_authority import ActorPolicy, SqlResourceAuthority, install_session
from app.benchmarks.trials import TrialStore
from app.models.benchmark_trial import (
    BenchmarkHostReservation,
    BenchmarkResourceLease,
    BenchmarkTrial,
)
from app.runtime.admission import GroupPolicy, ResourceDemand, host_capacity
from app.runtime.broker import ResourceLease
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.managed_attach import ManagedDockerProcess
from app.runtime.managed_docker import RootlessDockerRuntime
from app.runtime.managed_spec import (
    ContainerSpec,
    FinalizerSpec,
    OwnerScope,
    PathMapping,
    Quota,
    RuntimePolicy,
)
from app.runtime.reconcile import RootlessReconciler
from app.runtime.spec import BindMount
from evalhive_benchmark import ConnectorClient, ProtocolError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.runtime.authority_fixture import manifest

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_RECOVERY") != "1",
    reason="Requires explicit isolated PostgreSQL/rootless recovery checks",
)
WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000002")
PROVIDER = "fixture.rootless"


def request(scope, handle, operation="inspect", *, reconcile=True):
    return {
        "protocol_version": "1.0",
        "type": "broker_request",
        "id": uuid.uuid4().hex,
        "scope": {
            "workspace_id": scope.workspace_id,
            "trial_id": scope.trial_id,
            "generation": scope.generation,
        },
        "provider": PROVIDER,
        "operation": operation,
        "handle": handle,
        "payload": {"reconcile": True} if reconcile else {"resource_id": "environment"},
    }


@pytest_asyncio.fixture
async def native_trial(_engine, db_session, request):
    local = Path(os.environ["EVALHIVE_NATIVE_FIXTURE_DIR"])
    host = os.environ["EVALHIVE_NATIVE_FIXTURE_HOST"]
    client = docker.DockerClient(base_url="unix:///run/user/11001/docker.sock", timeout=15)
    bundle = local / "bundle"
    bundle.mkdir(exist_ok=True)
    artifact = client.containers.create(
        "evalhive-supervisor:development",
        entrypoint=["/evalhive-supervisor", "--capabilities"],
        labels={"evalhive.purpose": "benchmark-native-recovery-helper"},
    )
    try:
        chunks, _ = artifact.get_archive("/evalhive-supervisor")
        with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as archive:
            executable = archive.getmember("evalhive-supervisor")
            assert executable.isfile() and executable.size < 1024**2
            (bundle / "supervisor").write_bytes(archive.extractfile(executable).read())
            (bundle / "supervisor").chmod(0o755)
    finally:
        artifact.remove(force=True, v=True)
    runtime = RootlessDockerRuntime(
        client,
        RuntimePolicy(PathMapping(host, str(local)), (PathMapping(host + "/bundle", str(bundle)),)),
    )
    image = await asyncio.to_thread(runtime.resolve_image, "evalhive-environment-workspace:0.1.0")
    store = TrialStore(_engine, PrivateStore("native-recovery-fixture-key"))
    trial = await store.create(
        WORKSPACE,
        snapshot={"title": "Native recovery engineering fixture"},
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    owner = await store.claim(WORKSPACE, trial.id, uuid.uuid4())
    await store.transition(owner, "created", "planning")
    quota = Quota(256, 1, 128)

    async def measure():
        return await asyncio.to_thread(host_capacity, runtime, local)

    planning = getattr(request, "param", None) == "planner"
    await store.reserve(
        owner,
        demands=(ResourceDemand("environment", quota, 256, 1, 16),),
        native_resources={},
        measure=measure,
        runtime_policy=runtime.policy,
        group_policy=GroupPolicy(),
        stage="planning" if planning else "full",
    )
    scope = OwnerScope(str(WORKSPACE), str(trial.id), str(uuid.uuid4()), 1)
    handle = str(uuid.uuid4())
    spec = ContainerSpec(
        scope,
        handle,
        image["image_id"],
        role="worker"
        if getattr(request, "param", None) in {"worker", "planner"}
        else "environment",
        quota=quota,
        mounts=(BindMount(host + "/bundle", "/evalhive-runtime"),),
        command=(
            "python",
            "-c",
            "import time;open('/tmp/result.txt','w').write('native-evidence');time.sleep(300)",
        ),
        finalizer=FinalizerSpec(
            (
                "python",
                "-c",
                "import os;open('/tmp/finalized.txt','a').write(os.environ['EVALHIVE_RESOURCE_SCOPE']+'\\n')",
            ),
            15,
            True,
        )
        if getattr(request, "param", None) == "finalizer"
        else FinalizerSpec(
            ("python", "-c", "raise SystemExit(3)"),
            15,
            True,
        )
        if getattr(request, "param", None) == "finalizer-failure"
        else None,
    )
    lease = ResourceLease(handle, scope, "container", spec)
    actor = uuid.uuid4()
    await install_session(
        store,
        owner,
        scope,
        leases={"environment": (PROVIDER, lease)},
        actors={
            actor: ActorPolicy(
                (PROVIDER,),
                ("environment",),
                ("planning", "cleaning"),
                ("allocate", "exec", "inspect", "stop", "release"),
            )
        },
    )
    authority = SqlResourceAuthority(store, owner, scope.session_id, actor, PROVIDER)
    try:
        yield store, owner, scope, actor, lease, authority, runtime
    finally:
        # Only this fixture's exact owner/resource labels are ever searched.
        native = await asyncio.to_thread(runtime.find, scope, handle)
        if native is not None:
            await asyncio.to_thread(runtime.stop, native, grace_seconds=0, remove=True)
        client.close()


async def takeover(store, owner, actor, scope, engine):
    async with engine.begin() as connection:
        await connection.execute(
            update(BenchmarkTrial)
            .where(BenchmarkTrial.id == owner.trial_id)
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    recovered = await store.claim(WORKSPACE, owner.trial_id, uuid.uuid4())
    return recovered, SqlResourceAuthority(
        store, recovered, scope.session_id, actor, PROVIDER, recovery=True
    )


async def wait_for_file(runtime, native):
    from docker.errors import NotFound

    for _ in range(50):
        try:
            container = await asyncio.to_thread(runtime.container, native)
            chunks, _ = await asyncio.to_thread(container.get_archive, "/tmp/result.txt")
            data = await asyncio.to_thread(b"".join, chunks)
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                assert archive.extractfile("result.txt").read() == b"native-evidence"
            return
        except NotFound:
            await asyncio.sleep(0.05)
    pytest.fail("Native fixture did not produce its evidence file")


async def test_new_controller_finds_unconfirmed_container_and_preserves_evidence(
    native_trial, _engine
):
    store, owner, scope, actor, lease, authority, runtime = native_trial
    command = request(scope, lease.handle, "allocate", reconcile=False)
    command.pop("handle")
    with pytest.raises(ProtocolError, match="lost receipt"):
        async with authority.operation(command) as call:
            planned = await call.allocation("environment")
            await call.save(replace(planned, state="creating"))
            native = await asyncio.to_thread(runtime.create, planned.intent)
            raise ProtocolError("RESOURCE_UNCERTAIN", "Fixture lost receipt", "uncertain")
    await wait_for_file(runtime, native)
    recovered, recovery_authority = await takeover(store, owner, actor, scope, _engine)
    reconciler = RootlessReconciler(runtime, recovery_authority, PROVIDER)
    assert (await reconciler.handle(request(scope, lease.handle)))["state"] == "allocated"
    assert recovered.generation == 2 and native.scope.generation == 1
    with pytest.raises(ProtocolError, match="no longer owns"):
        await RootlessReconciler(runtime, authority, PROVIDER).handle(
            request(scope, lease.handle, "release")
        )
    recovery = RecoveryService(
        store, actor, {PROVIDER: lambda value: RootlessReconciler(runtime, value, PROVIDER)}
    )
    assert (await recovery.sweep(recovered))[0]["state"] == "stopped"
    assert (await asyncio.to_thread(runtime.inspect, native))["status"] == "exited"
    await wait_for_file(runtime, native)  # Stopped rootfs remains available for capture.
    assert (await recovery.sweep(recovered, "release"))[0]["state"] == "released"
    assert await recovery.sweep(recovered, "release") == []
    release = request(scope, lease.handle, "release")
    assert (await reconciler.handle(release))["state"] == "released"
    assert (await reconciler.handle(release))["state"] == "released"
    assert await asyncio.to_thread(runtime.find, scope, lease.handle) is None
    await store.transition(recovered, "cleaning", "finished")
    async with AsyncSession(_engine) as db:
        assert await db.get(BenchmarkHostReservation, store.host_id) is None
        assert (await db.get(BenchmarkResourceLease, uuid.UUID(lease.handle))).state == "released"


async def test_late_native_create_keeps_reservation_until_actual_resource_is_reconciled(
    native_trial, _engine
):
    store, owner, scope, actor, lease, authority, runtime = native_trial
    start = threading.Event()
    command = request(scope, lease.handle, "allocate", reconcile=False)
    command.pop("handle")

    def late_create():
        if not start.wait(20):
            raise TimeoutError("Native fixture was not released")
        return runtime.create(lease.intent)

    pending = None
    try:
        with pytest.raises(ProtocolError):
            async with authority.operation(command) as call:
                await call.save(replace(await call.allocation("environment"), state="creating"))
                pending = asyncio.create_task(asyncio.to_thread(late_create))
                raise ProtocolError(
                    "RESOURCE_UNCERTAIN", "Fixture disconnected during native create", "uncertain"
                )
        recovered, recovery_authority = await takeover(store, owner, actor, scope, _engine)
        reconciler = RootlessReconciler(runtime, recovery_authority, PROVIDER)
        assert not pending.done()
        with pytest.raises(ProtocolError) as absent:
            await reconciler.handle(request(scope, lease.handle, "release"))
        assert absent.value.kind == "uncertain"
        with pytest.raises(ProtocolError, match="remain unreconciled"):
            await store.transition(recovered, "cleaning", "finished")
        async with AsyncSession(_engine) as db:
            assert (
                await db.get(BenchmarkHostReservation, store.host_id)
            ).trial_id == owner.trial_id
        start.set()
        native = await asyncio.wait_for(asyncio.shield(pending), 15)
        assert native.scope.generation == 1
        recovery = RecoveryService(
            store, actor, {PROVIDER: lambda value: RootlessReconciler(runtime, value, PROVIDER)}
        )
        assert (await recovery.sweep(recovered, "release"))[0]["state"] == "released"
        assert await asyncio.to_thread(runtime.find, scope, lease.handle) is None
        await store.transition(recovered, "cleaning", "finished")
    finally:
        start.set()
        if pending is not None:
            await asyncio.wait_for(asyncio.shield(pending), 20)


@pytest.mark.parametrize("native_trial", ["worker", "planner"], indirect=True)
async def test_old_worker_transport_detaches_after_takeover_without_stopping_new_owner(
    native_trial, _engine
):
    store, owner, scope, actor, lease, authority, runtime = native_trial
    provider = RootlessDockerProvider(runtime, authority, manifest())
    allocate = request(scope, lease.handle, "allocate", reconcile=False)
    allocate.pop("handle")
    await provider.handle(allocate)
    native = await asyncio.to_thread(runtime.find, scope, lease.handle)
    attempts = []

    async def old_control(operation):
        attempts.append(operation)
        return await RootlessReconciler(runtime, authority, PROVIDER).handle(
            request(scope, lease.handle, operation)
        )

    transport = await ManagedDockerProcess.attach(runtime, native, control=old_control)
    client = ConnectorClient(transport)
    try:
        recovered, _ = await takeover(store, owner, actor, scope, _engine)
        with pytest.raises(ProtocolError) as detached:
            await asyncio.wait_for(client.close(0.1, shutdown=False), 4)
        assert detached.value.code == "TRANSPORT_CLOSE_INCOMPLETE"
        assert attempts == ["stop", "stop"]
        assert (await asyncio.to_thread(runtime.inspect, native))["status"] == "running"
        async with AsyncSession(_engine) as db:
            row = await db.get(BenchmarkResourceLease, uuid.UUID(lease.handle))
            assert row.state == "allocated" and row.origin_generation == 1
            assert (await db.get(BenchmarkTrial, owner.trial_id)).generation == 2
        recovery = RecoveryService(
            store, actor, {PROVIDER: lambda value: RootlessReconciler(runtime, value, PROVIDER)}
        )
        assert (await recovery.sweep(recovered, "release"))[0]["state"] == "released"
        await store.transition(recovered, "cleaning", "finished")
        assert await asyncio.to_thread(runtime.find, scope, lease.handle) is None
    finally:
        await transport.aclose()


@pytest.mark.parametrize("native_trial", ["planner"], indirect=True)
async def test_isolated_planner_is_retained_during_native_group_finalization(native_trial):
    store, owner, scope, actor, lease, authority, runtime = native_trial
    provider = RootlessDockerProvider(runtime, authority, manifest())
    allocate = request(scope, lease.handle, "allocate", reconcile=False)
    allocate.pop("handle")
    await provider.handle(allocate)
    worker = await asyncio.to_thread(runtime.find, scope, lease.handle)
    await wait_for_file(runtime, worker)
    with pytest.raises(ProtocolError) as no_preparation:
        await store.transition(owner, "planning", "preparing")
    assert no_preparation.value.code == "GROUP_ADMISSION_REQUIRED"

    async def measure():
        return await asyncio.to_thread(host_capacity, runtime, Path(runtime.policy.data.local))

    quota = lease.intent.quota
    budget = await store.reserve(
        owner,
        demands=(
            ResourceDemand("environment", quota, 256, 1, 16),
            ResourceDemand("task", quota, 256, 1, 16),
        ),
        native_resources={},
        measure=measure,
        runtime_policy=runtime.policy,
        group_policy=GroupPolicy(),
    )
    assert budget["stage"] == "full"
    assert await asyncio.to_thread(runtime.find, scope, lease.handle) == worker
    await store.transition(owner, "planning", "preparing")
    task_scope = replace(scope, session_id=str(uuid.uuid4()))
    task_id = str(uuid.uuid4())
    task_spec = replace(lease.intent, scope=task_scope, resource_id=task_id, role="environment")
    task = ResourceLease(task_id, task_scope, "container", task_spec)
    await install_session(
        store,
        owner,
        task_scope,
        leases={"task": (PROVIDER, task)},
        actors={
            actor: ActorPolicy(
                (PROVIDER,),
                ("task",),
                ("preparing", "cleaning"),
                ("allocate", "inspect", "release"),
            )
        },
    )
    task_authority = SqlResourceAuthority(store, owner, task_scope.session_id, actor, PROVIDER)
    task_allocate = request(task_scope, task_id, "allocate", reconcile=False)
    task_allocate.pop("handle")
    task_allocate["payload"] = {"resource_id": "task"}
    try:
        await RootlessDockerProvider(runtime, task_authority, manifest()).handle(task_allocate)
        task_native = await asyncio.to_thread(runtime.find, task_scope, task_id)
        await wait_for_file(runtime, task_native)
        assert task_native != worker
        await store.transition(owner, "preparing", "cleaning")
        recovery = RecoveryService(
            store, actor, {PROVIDER: lambda value: RootlessReconciler(runtime, value, PROVIDER)}
        )
        released = await recovery.sweep(owner, "release")
        assert len(released) == 2 and all(value["state"] == "released" for value in released)
        await store.transition(owner, "cleaning", "finished")
    finally:
        remaining = await asyncio.to_thread(runtime.find, task_scope, task_id)
        if remaining is not None:
            await asyncio.to_thread(runtime.stop, remaining, grace_seconds=0, remove=True)


@pytest.mark.parametrize("completed", [True, False])
async def test_durable_deadline_survives_transport_loss_and_retains_completed_services(
    native_trial, completed
):
    store, owner, scope, actor, lease, authority, runtime = native_trial
    provider = RootlessDockerProvider(runtime, authority, manifest())
    allocate = request(scope, lease.handle, "allocate", reconcile=False)
    allocate.pop("handle")
    await provider.handle(allocate)
    native = await asyncio.to_thread(runtime.find, scope, lease.handle)
    program = """
import os,signal,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
child=os.fork()
if child==0:
 os.setsid()
 while True: time.sleep(1)
print('{"ready":true}',flush=True)
""" + ("" if completed else "while True: time.sleep(1)\n")
    execute = request(scope, lease.handle, "exec", reconcile=False)
    execute["payload"] = {
        "argv": ["python", "-c", program],
        "mode": "attach",
        "timeout_seconds": 60,
    }
    result = await provider.handle(execute)
    inspect = request(scope, result["handle"], "inspect", reconcile=False)
    inspect["payload"] = {}
    process = await provider.open_transport(inspect)
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b'{"ready":true}\n'
        if completed:
            assert await asyncio.wait_for(process.wait(), 10) == 0
        # Disconnect the caller; the durable host deadline still owns its exec.
        await process.aclose()
        update_deadline = request(scope, result["handle"], "inspect", reconcile=False)
        update_deadline["payload"] = {}
        async with authority.operation(update_deadline) as call:
            owned = await call.resource(result["handle"])
            await call.save(
                replace(owned, deadline_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
            )
            await call.complete({"deadline_fixture": True})
        watchdog = DeadlineService(
            store, PROVIDER, actor, lambda value: RootlessReconciler(runtime, value, PROVIDER)
        )
        outcomes = await watchdog.sweep(owner)
        assert len(outcomes) == 1 and "error" not in outcomes[0]
        outcome = outcomes[0]
        assert outcome["deadline_expired"]
        assert outcome["timed_out"] == (not completed)
        assert outcome["state"] == ("retained" if completed else "stopped")
        status = await asyncio.to_thread(provider.owned.control, owned.native, "status")
        assert status["children"] == (1 if completed else 0)
        assert (await asyncio.to_thread(runtime.inspect, native))["status"] == "running"
        await wait_for_file(runtime, native)
        assert await watchdog.sweep(owner) == []
        await store.transition(owner, "planning", "cleaning")
        recovery = RecoveryService(
            store, actor, {PROVIDER: lambda value: RootlessReconciler(runtime, value, PROVIDER)}
        )
        released = await recovery.sweep(owner, "release")
        assert [value["kind"] for value in released] == ["exec", "container"]
        assert all(value["state"] == "released" for value in released)
        await provider.close()
        await store.transition(owner, "cleaning", "finished")
    finally:
        await process.aclose()
        await provider.close()
