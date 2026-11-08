"""SDK worker + durable broker + rootless resources, without backend in worker."""

import asyncio
import hashlib
import io
import json
import os
import tarfile
import uuid
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import docker
import pytest
import pytest_asyncio
from app.benchmarks.deadlines import DeadlineService
from app.benchmarks.driver import DriverLimits, TrialDriver
from app.benchmarks.private_store import PrivateStore
from app.benchmarks.recovery import RecoveryService
from app.benchmarks.trials import TrialStore
from app.benchmarks.worker_host import RootlessWorkerHost
from app.models.benchmark_trial import (
    BenchmarkEnvironmentSession,
    BenchmarkJournalEntry,
    BenchmarkOperation,
    BenchmarkResourceLease,
    BenchmarkTrial,
)
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.lease_codec import decode_lease
from app.runtime.managed_docker import RootlessDockerRuntime
from app.runtime.managed_exec import OwnedExecRuntime
from app.runtime.managed_spec import ManagedRuntimeError, PathMapping, Quota, RuntimePolicy
from app.runtime.reconcile import RootlessReconciler
from app.runtime.registered_assets import RegisteredFile, RootlessAssets
from evalhive_benchmark import ProtocolError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import undefer

from tests.fixtures.benchmark_execution_outcome import fixture_execution_outcome
from tests.runtime.agent_worker_fixture import AgentWorker
from tests.runtime.authority_fixture import FixtureArtifacts, manifest
from tests.runtime.broker_worker_fixture import CASE, Worker
from tests.runtime.regrade_worker_fixture import RegradeWorker

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_WORKER_HOST") != "1",
    reason="Requires explicit isolated worker/controller checks",
)
WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest_asyncio.fixture
async def native_engine(_engine, db_session):
    # Production reuses connections. Repeated NullPool/SCRAM handshakes from
    # heartbeat, events and native-state polling otherwise dominate one CPU.
    pooled = create_async_engine(_engine.url, pool_size=5, max_overflow=5)
    try:
        yield pooled
    finally:
        await pooled.dispose()


@pytest.mark.parametrize(
    "automatic",
    [
        False,
        True,
        "harness-pi",
        "harness-opencode",
        "cancel",
        "recover",
        "recover_active",
        pytest.param(
            "evidence",
            marks=pytest.mark.skipif(
                not os.getenv("EVALHIVE_EVIDENCE_MINIO"),
                reason="Isolated MinIO fixture not configured",
            ),
        ),
        pytest.param(
            "regrade",
            marks=pytest.mark.skipif(
                not os.getenv("EVALHIVE_EVIDENCE_MINIO"),
                reason="Isolated MinIO fixture not configured",
            ),
        ),
    ],
)
async def test_real_worker_plans_prepares_executes_and_captures_via_durable_broker(
    native_engine, automatic
):
    _engine = native_engine
    harness_connector = (
        automatic.removeprefix("harness-")
        if isinstance(automatic, str) and automatic.startswith("harness-")
        else None
    )
    local = Path(os.environ["EVALHIVE_NATIVE_FIXTURE_DIR"])
    host_root = os.environ["EVALHIVE_NATIVE_FIXTURE_HOST"]
    bundle = local / "bundle"
    bundle.mkdir(exist_ok=True)
    docker_client = docker.DockerClient(base_url="unix:///run/user/11001/docker.sock", timeout=15)
    artifact = docker_client.containers.create(
        "evalhive-supervisor:development",
        entrypoint=["/evalhive-supervisor", "--capabilities"],
        labels={"evalhive.purpose": "benchmark-worker-host-helper"},
    )
    try:
        chunks, _ = artifact.get_archive("/evalhive-supervisor")
        with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as archive:
            member = archive.getmember("evalhive-supervisor")
            assert member.isfile() and member.size < 1024**2
            (bundle / "supervisor").write_bytes(archive.extractfile(member).read())
            (bundle / "supervisor").chmod(0o755)
    finally:
        artifact.remove(force=True, v=True)
    runtime = RootlessDockerRuntime(
        docker_client,
        RuntimePolicy(
            PathMapping(host_root, str(local)), (PathMapping(host_root + "/bundle", str(bundle)),)
        ),
    )
    worker_class = (
        AgentWorker if harness_connector else (RegradeWorker if automatic == "regrade" else Worker)
    )
    worker_image = await asyncio.to_thread(
        runtime.resolve_image,
        "evalhive-broker-worker-fixture:b08-agent"
        if harness_connector
        else "evalhive-benchmark-regrade-fixture:b05-inputs"
        if automatic == "regrade"
        else "evalhive-broker-worker-fixture:b05-stages",
    )
    task_image = await asyncio.to_thread(
        runtime.resolve_image, "evalhive-environment-workspace:0.1.0"
    )
    installed_bundle, harness_manifest = None, None
    if harness_connector:
        from app.harnesses.catalog import get_manifest
        from app.runtime.bundles import install_bundle

        harness_manifest = get_manifest(harness_connector)
        installation = deepcopy(harness_manifest)
        installation["requirements"]["runtime_bundle"]["image"] = (
            "evalhive-" + harness_connector + "-bundle:development"
        )
        installed_bundle = await asyncio.to_thread(
            install_bundle, runtime, installation, PathMapping(host_root + "/bundle", str(bundle))
        )
    declaration = deepcopy(worker_class.manifest)
    declaration["image"] = worker_image["image_id"]
    registration = {
        "format_version": "1.0",
        "connector_id": declaration["id"],
        "connector_version": declaration["version"],
        "provider_id": "fixture.rootless",
        "worker_asset": "fixture.worker",
        "assets": {
            "fixture.worker": {
                "kind": "container",
                "role": "worker",
                "image": worker_image["image_id"],
                "limits": asdict(Quota(512, 0.25, 128)),
                "minimum": {"memory_mb": 512, "cpu_cores": 0.25, "disk_mb": 16},
                "environment": {"EVALHIVE_DURABLE_BROKER": "1"},
            },
            "fixture.task": {
                "kind": "container",
                "role": "environment",
                "image": task_image["image_id"],
                "limits": asdict(Quota(256, 1, 128)),
                "minimum": {"memory_mb": 256, "cpu_cores": 1, "disk_mb": 16},
                "command": ["python", "-c", "import time;time.sleep(300)"],
                "mounts": [
                    {"file_id": "supervisor", "target": "/evalhive-runtime", "read_only": True}
                ],
                "outputs": {
                    "result": {
                        "path": "/workspace/result.txt",
                        "visibility": "user",
                        "required": True,
                        "max_archive_bytes": 2 * 1024**2,
                    }
                },
            },
        },
    }
    if harness_connector:
        registration["assets"]["fixture.task"]["limits"] = asdict(Quota(1024, 1, 256))
    if automatic in ("evidence", "regrade"):
        registration["assets"]["fixture.task"]["outputs"]["grade_log"] = {
            "path": "/workspace/grader.txt",
            "visibility": "evaluator",
            "required": True,
            "stage": "evaluation",
            "max_archive_bytes": 2 * 1024**2,
        }
    if automatic == "regrade":
        registration["assets"]["fixture.task"]["minimum"]["disk_mb"] = 48
        registration["assets"]["fixture.task"]["outputs"]["world"] = {
            "path": "/workspace",
            "visibility": "private",
            "required": True,
            "max_archive_bytes": 64 * 1024**2,
        }
    assets = RootlessAssets(
        registration,
        runtime,
        files={
            "supervisor": RegisteredFile(
                installed_bundle.host_path if installed_bundle else host_root + "/bundle",
                "sha256:"
                + (
                    installed_bundle.fingerprint
                    if installed_bundle
                    else hashlib.sha256((bundle / "supervisor").read_bytes()).hexdigest()
                ),
            )
        },
        connector_id=declaration["id"],
        connector_version=declaration["version"],
    )
    store = TrialStore(_engine, PrivateStore("native-worker-host-fixture-key"))
    task_id = None
    if harness_connector:
        from app.models.task import Task

        async with AsyncSession(_engine) as db:
            task = Task(
                title="Native benchmark Harness fixture",
                workspace_id=WORKSPACE,
                status="in_progress",
            )
            db.add(task)
            await db.flush()
            task_id = task.id
            await db.commit()
    trial = await store.create(
        WORKSPACE,
        task_id=task_id,
        snapshot={"fixture": "native worker host"},
        deadline_at=datetime.now(UTC)
        + timedelta(seconds=180 if automatic == "regrade" or harness_connector else 55),
    )
    owner = await store.claim(WORKSPACE, trial.id, uuid.uuid4())
    if automatic in (False, "recover"):
        await store.transition(owner, "created", "planning")

    evidence = None
    if automatic in ("evidence", "regrade"):
        import urllib3
        from app.benchmarks.evidence import EvidenceStore
        from minio import Minio

        client = Minio(
            os.environ["EVALHIVE_EVIDENCE_MINIO"],
            access_key="fixture-evidence",
            secret_key="fixture-evidence-password",
            secure=False,
            http_client=urllib3.PoolManager(
                timeout=urllib3.Timeout(connect=3, read=5), retries=False
            ),
        )
        bucket = "native-evidence-" + uuid.uuid4().hex
        await asyncio.to_thread(client.make_bucket, bucket)
        evidence = EvidenceStore(store, client, bucket=bucket)

    class Capture(FixtureArtifacts):
        def __init__(self, selected_evidence=evidence, selected_owner=owner):
            super().__init__()
            self.receipts = {}
            self.evidence, self.owner = selected_evidence, selected_owner

        async def save(self, scope, grant, chunks, *, publication=None):
            if grant.artifact_id == "result" and self.owner.control_id is None:
                async with AsyncSession(_engine) as db:
                    rows = (
                        await db.scalars(
                            select(BenchmarkResourceLease)
                            .where(BenchmarkResourceLease.kind == "exec")
                            .options(undefer(BenchmarkResourceLease.private_value))
                        )
                    ).all()
                    retained = [row for row in rows if row.state == "retained"]
                    assert retained
                    leases = [
                        decode_lease(store.private.open("lease:" + str(row.id), row.private_value))
                        for row in retained
                    ]
                statuses = [
                    await asyncio.to_thread(
                        OwnedExecRuntime(runtime).control, lease.native, "status"
                    )
                    for lease in leases
                ]
                assert any(status["main_done"] and status["children"] == 1 for status in statuses)
            if self.evidence is None:
                return await super().save(scope, grant, chunks, publication=publication)
            data = bytearray()

            async def observed():
                async for chunk in chunks:
                    data.extend(chunk)
                    yield chunk

            saved = await self.evidence.sink(self.owner).save(
                scope, grant, observed(), publication=publication
            )
            self.values[grant.artifact_id] = bytes(data)
            self.receipts[grant.artifact_id] = saved
            return saved

    artifacts = Capture()
    capture_validator = (
        evidence.validate_capture if evidence is not None else artifacts.validate_capture
    )
    factories = {
        "fixture.rootless": lambda authority: RootlessDockerProvider(
            runtime, authority, manifest(), artifacts=artifacts
        )
    }
    agent_key = uuid.uuid4().hex
    agent_factory = None
    if harness_connector:
        from app.benchmarks.agent_calls import AgentCallStore
        from app.benchmarks.agent_gateway import AgentGateway
        from app.benchmarks.harness_execution import HarnessExecution
        from app.benchmarks.harness_transport import RootlessHarnessTransport
        from evalhive_runner.runner import RunnerPolicy

        def agent_factory(host):
            calls = AgentCallStore(store, owner, host.worker_actor)
            harness_spec = {
                "protocol_version": "1.0",
                "workspace_id": str(WORKSPACE),
                "task_id": str(task_id),
                "attempt_id": str(calls.session_id),
                "task": {"prompt": "Native fixture"},
                "cwd": "/workspace",
                "config": {},
                "limits": {"max_turns": 4},
                "model": {
                    "name": "fake-model",
                    "endpoint": "http://127.0.0.1:8765/v1",
                    "credential_ref": "model_api_key",
                },
                "metadata": {
                    "image_id": task_image["image_id"],
                    "runtime_bundle_image_id": installed_bundle.image_id,
                    "runtime_bundle_fingerprint": installed_bundle.fingerprint,
                },
            }

            async def transport():
                return await RootlessHarnessTransport(
                    host,
                    host.plan,
                    host.prepared,
                    installed_bundle,
                    harness_manifest,
                    credential_ref="agent.credentials",
                )()

            return AgentGateway(
                calls,
                HarnessExecution(
                    calls,
                    harness_manifest,
                    harness_spec,
                    transport,
                    local / "harness-spool" / str(calls.session_id) / "events.sqlite",
                    private_values=[agent_key],
                    policy=RunnerPolicy(
                        heartbeat_seconds=0.2, startup_seconds=20, command_seconds=20
                    ),
                ),
            )

    worker_host = RootlessWorkerHost(
        store,
        owner,
        declaration,
        assets,
        factories,
        agent_factory=agent_factory,
        host_private_inputs={"agent.credentials": json.dumps({"model_api_key": agent_key}).encode()}
        if harness_connector
        else None,
    )
    hosts = [worker_host]
    source_alias = None
    if automatic == "regrade":
        source_alias = "evalhive-regrade-source-fixture:" + uuid.uuid4().hex
        await asyncio.to_thread(
            docker_client.images.get(worker_image["image_id"]).tag, source_alias
        )
        declaration["image"] = source_alias
    spec = {
        "protocol_version": "1.0",
        "scope": {
            "workspace_id": str(owner.workspace_id),
            "trial_id": str(owner.trial_id),
            "generation": owner.generation,
        },
        "case": CASE,
        "config": {"grader_artifact": True} if automatic in ("evidence", "regrade") else {},
        "credential_refs": {},
        "models": {},
        "limits": {"wall_time_seconds": 60, "memory_mb": 768, "cpu_cores": 1.25, "disk_mb": 32},
    }
    if harness_connector:
        spec["limits"].update(memory_mb=1536, disk_mb=64, wall_time_seconds=180)
    if automatic == "regrade":
        spec["limits"]["disk_mb"] = 192
        spec["limits"]["wall_time_seconds"] = 180
    try:
        if (
            automatic in (True, "cancel", "recover_active", "evidence", "regrade")
            or harness_connector
        ):
            recovery = RecoveryService(
                store,
                worker_host.host_actor,
                {
                    "fixture.rootless": lambda authority: RootlessReconciler(
                        runtime, authority, "fixture.rootless"
                    )
                },
            )
            watchdog = DeadlineService(
                store,
                "fixture.rootless",
                worker_host.host_actor,
                lambda authority: RootlessReconciler(runtime, authority, "fixture.rootless"),
            )
            controller = TrialDriver(
                worker_host,
                recovery,
                capture_providers=factories,
                capture_validator=capture_validator,
                execution_outcome=None if harness_connector else fixture_execution_outcome(owner),
                deadlines=(watchdog,),
                limits=DriverLimits(
                    phase_seconds=90 if automatic == "regrade" or harness_connector else 35,
                    stop_seconds=1,
                    capture_seconds=10,
                    cleanup_seconds=25,
                    service_seconds=10,
                    heartbeat_seconds=5,
                ),
            )
            if automatic in ("cancel", "recover_active"):
                spec["config"] = {"wait_drive": True}
            pending = asyncio.create_task(
                controller.run(
                    spec,
                    private_inputs={
                        "fixture.alpha": b"alpha-private-fixture",
                        "fixture.zeta": b"zeta-private-fixture",
                    },
                )
            )
            try:
                if automatic in ("cancel", "recover_active"):
                    from docker.errors import NotFound

                    async with asyncio.timeout(25):
                        while True:
                            if worker_host.environment:
                                selected = worker_host.environment
                                native = await asyncio.to_thread(
                                    runtime.find, selected.scope, selected.leases["task"][1].handle
                                )
                                if native:
                                    container = await asyncio.to_thread(runtime.container, native)
                                    try:
                                        await asyncio.to_thread(
                                            container.get_archive, "/tmp/agent.started"
                                        )
                                        break
                                    except NotFound:
                                        pass
                            if pending.done():
                                pytest.fail(
                                    "Automatic native run ended before agent startup: "
                                    + str(await pending)
                                )
                            await asyncio.sleep(0.1)
                    if automatic == "recover_active":
                        async with _engine.begin() as connection:
                            await connection.execute(
                                update(BenchmarkTrial)
                                .where(BenchmarkTrial.id == owner.trial_id)
                                .values(
                                    controller_expires_at=datetime.now(UTC) - timedelta(seconds=1)
                                )
                            )
                        claimed = await store.claim(WORKSPACE, owner.trial_id, uuid.uuid4())
                        new_host = RootlessWorkerHost(
                            store, claimed, declaration, assets, factories
                        )
                        next_recovery = RecoveryService(
                            store,
                            new_host.host_actor,
                            {
                                "fixture.rootless": lambda authority: RootlessReconciler(
                                    runtime, authority, "fixture.rootless"
                                )
                            },
                        )
                        recovered = await TrialDriver(
                            new_host,
                            next_recovery,
                            capture_providers=factories,
                            capture_validator=artifacts.validate_capture,
                            execution_outcome=fixture_execution_outcome(claimed),
                        ).run(spec)
                        old = await asyncio.wait_for(pending, 12)
                        assert old["controller"] == "detached"
                        assert recovered["phase"] == "finished" and recovered["execution"] == "lost"
                        assert recovered["recording"] == "incomplete" and not new_host.started
                        assert any(key.startswith("recovery.") for key in artifacts.values)
                        return
                    assert await store.request_cancel(WORKSPACE, owner.trial_id)
                result = await asyncio.wait_for(
                    pending,
                    150
                    if automatic == "regrade" or harness_connector
                    else (20 if automatic == "cancel" else 45),
                )
                assert result["phase"] == "finished" and result["cleanup"] == "complete"
                assert result["execution"] == ("cancelled" if automatic == "cancel" else "success")
                assert result["evaluation"] == (
                    "unavailable" if automatic == "cancel" else "completed"
                )
                with tarfile.open(fileobj=io.BytesIO(artifacts.values["result"])) as archive:
                    content = archive.extractfile("result.txt").read()
                    assert content.startswith(b"Prepared through the Benchmark SDK\n")
                    assert (b"Driven through the Benchmark SDK\n" in content) == (
                        automatic in (True, "evidence", "regrade") or harness_connector is not None
                    )
                if harness_connector:
                    from app.models.execution_attempt import ExecutionAttempt, ExecutionEvent

                    async with AsyncSession(_engine) as db:
                        attempt = await db.get(
                            ExecutionAttempt, worker_host.agent_gateway.calls.session_id
                        )
                        assert attempt.status == "success", attempt.result
                        assert attempt.connector_id == harness_connector
                        assert attempt.run_spec["cwd"] == "/workspace"
                        assert attempt.run_spec["metadata"]["image_id"] == task_image["image_id"]
                        events = list(
                            await db.scalars(
                                select(ExecutionEvent).where(
                                    ExecutionEvent.attempt_id == attempt.id
                                )
                            )
                        )
                        assert any(event.event == "tool.end" for event in events)
                        assert agent_key not in json.dumps([event.payload for event in events])
                        assert worker_host.host_auxiliary_disk_mb == 24
                if evidence is not None:
                    async with evidence.open_verified(
                        WORKSPACE,
                        owner.trial_id,
                        artifacts.receipts["result"],
                        visibilities={"user"},
                    ) as stream:
                        assert stream.read() == artifacts.values["result"]
                    async with evidence.open_verified(
                        WORKSPACE,
                        owner.trial_id,
                        artifacts.receipts["grade_log"],
                        visibilities={"evaluator"},
                    ) as stream:
                        with tarfile.open(fileobj=stream) as archive:
                            assert (
                                archive.extractfile("grader.txt").read()
                                == b"native checker completed\n"
                            )
                if automatic == "regrade":
                    from app.benchmarks.evaluation_controls import EvaluationControls
                    from app.benchmarks.evidence import EvidenceStore
                    from app.benchmarks.regrade_driver import RegradeDriver
                    from app.benchmarks.retained_installation import load_retained_installation
                    from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
                    from app.models.benchmark_evaluation_input import BenchmarkEvaluationInput

                    async with AsyncSession(_engine) as db:
                        source = await db.scalar(select(BenchmarkEvaluationAttempt))
                        before = (await db.get(BenchmarkTrial, owner.trial_id)).next_seq
                        source_digest = source.result_digest
                    grading = EvaluationControls(store, evidence)
                    download_calls, original_get = [], evidence.client.get_object

                    def count_downloads(*args, **kwargs):
                        download_calls.append(True)
                        return original_get(*args, **kwargs)

                    evidence.client.get_object = count_downloads
                    grading_owner = await grading.create(
                        WORKSPACE,
                        owner.trial_id,
                        source.id,
                        uuid.uuid4(),
                        deadline_at=datetime.now(UTC) + timedelta(seconds=120),
                    )
                    grading_evidence = EvidenceStore(
                        grading, evidence.client, bucket=evidence.bucket
                    )
                    original_binary = (bundle / "supervisor").read_bytes()
                    try:
                        (bundle / "supervisor").write_bytes(b"changed installed fixture")
                        with pytest.raises(ProtocolError) as changed:
                            await load_retained_installation(grading, grading_owner, runtime)
                        assert changed.value.code == "STATIC_INPUT_CONFLICT"
                    finally:
                        (bundle / "supervisor").write_bytes(original_binary)
                    frozen = await load_retained_installation(grading, grading_owner, runtime)
                    assert download_calls == []
                    with patch.object(
                        runtime,
                        "resolve_image",
                        side_effect=ManagedRuntimeError("fixture image unavailable"),
                    ):
                        with pytest.raises(ProtocolError) as missing_image:
                            await load_retained_installation(grading, grading_owner, runtime)
                        assert missing_image.value.code == "PINNED_IMAGE_UNAVAILABLE"
                    assert frozen.worker_image_id == worker_image["image_id"]
                    await asyncio.to_thread(
                        docker_client.images.get(task_image["image_id"]).tag, source_alias
                    )
                    assert (await asyncio.to_thread(runtime.resolve_image, source_alias))[
                        "image_id"
                    ] != frozen.worker_image_id
                    artifacts = Capture(grading_evidence, grading_owner)
                    fresh_host = RootlessWorkerHost(
                        grading,
                        grading_owner,
                        frozen.manifest,
                        frozen.assets,
                        factories,
                        retained_evidence=grading_evidence,
                        worker_image_pin=frozen.worker_image_id,
                    )
                    hosts.append(fresh_host)
                    fresh_recovery = RecoveryService(
                        grading,
                        fresh_host.host_actor,
                        {
                            "fixture.rootless": lambda authority: RootlessReconciler(
                                runtime, authority, "fixture.rootless"
                            )
                        },
                    )
                    evaluated = await RegradeDriver(
                        fresh_host,
                        fresh_recovery,
                        capture_providers=factories,
                        capture_validator=grading_evidence.validate_capture,
                        limits=controller.limits,
                    ).run()
                    assert (
                        evaluated["phase"] == "finished"
                        and evaluated["evaluation"] == "completed"
                        and evaluated["cleanup"] == "complete"
                    )
                    assert (
                        fresh_host.environment.scope.session_id
                        != worker_host.environment.scope.session_id
                    )
                    async with AsyncSession(_engine) as db:
                        attempt = await db.get(BenchmarkEvaluationAttempt, grading_owner.control_id)
                        assert attempt.success is True and attempt.source_evaluation_id == source.id
                        assert (
                            await db.get(BenchmarkEvaluationAttempt, source.id)
                        ).result_digest == source_digest
                        assert (await db.get(BenchmarkTrial, owner.trial_id)).next_seq == before
                        cache = (await db.scalars(select(BenchmarkEvaluationInput))).all()
                        assert (
                            len(cache) == 1
                            and cache[0].state == "released"
                            and cache[0].size_bytes > 32 * 1024**2
                        )
                        from app.models.benchmark_evaluation_control import (
                            BenchmarkEvaluationJournal,
                        )

                        events = (await db.scalars(select(BenchmarkEvaluationJournal.event))).all()
                        assert "agent.output" not in events
                        assert len((await db.scalars(select(BenchmarkTrial))).all()) == 1
                async with AsyncSession(_engine) as db:
                    assert all(
                        row.state == "released"
                        for row in (await db.scalars(select(BenchmarkResourceLease))).all()
                    )
                return
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        channel = await worker_host.start()
        assert await worker_host.start() is channel
        assert (await channel.invoke("validate", {"spec": spec}))["accepted"]
        plan = await channel.invoke("plan", {"spec": spec})
        for key, value in {"memory_mb": 767, "cpu_cores": 1.24, "disk_mb": 31}.items():
            smaller = {**spec, "limits": {**spec["limits"], key: value}}
            with pytest.raises(ProtocolError) as over_budget:
                await worker_host.install_plan(smaller, plan)
            assert over_budget.value.code == "LIMIT_EXCEEDED"
        compiled = await worker_host.install_plan(
            spec,
            plan,
            private_inputs={
                "fixture.alpha": b"alpha-private-fixture",
                "fixture.zeta": b"zeta-private-fixture",
            },
        )
        await store.transition(owner, "planning", "preparing")
        prepared = await channel.invoke("prepare", {"spec": spec, "plan": plan}, timeout=30)
        assert prepared["ready"] and prepared["native_state"]["isolated"]
        if automatic == "recover":
            async with _engine.begin() as connection:
                await connection.execute(
                    update(BenchmarkTrial)
                    .where(BenchmarkTrial.id == owner.trial_id)
                    .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
            claimed = await store.claim(WORKSPACE, owner.trial_id, uuid.uuid4())
            new_host = RootlessWorkerHost(store, claimed, declaration, assets, factories)
            recovery = RecoveryService(
                store,
                new_host.host_actor,
                {
                    "fixture.rootless": lambda authority: RootlessReconciler(
                        runtime, authority, "fixture.rootless"
                    )
                },
            )
            controller = TrialDriver(
                new_host,
                recovery,
                capture_providers=factories,
                capture_validator=artifacts.validate_capture,
                execution_outcome=fixture_execution_outcome(claimed),
            )
            result = await controller.run(spec)
            assert result["phase"] == "finished" and result["execution"] == "lost"
            assert result["recording"] == "incomplete" and result["cleanup"] == "complete"
            assert not new_host.started and new_host.channel is None
            assert artifacts.values
            async with AsyncSession(_engine) as db:
                assert (await db.get(BenchmarkTrial, owner.trial_id)).generation == 2
            return
        await store.transition(owner, "preparing", "ready")
        await store.transition(owner, "ready", "driving")
        scoped = {"scope": spec["scope"], "payload": {"session_handle": prepared["session_handle"]}}
        assert (await channel.invoke("drive", scoped, timeout=30))["finished"]
        await store.finish_execution(owner, "success")
        await store.transition(owner, "driving", "capturing")
        captured = await channel.invoke("capture", scoped, timeout=30)
        assert captured["artifacts"][0]["visibility"] == "user"
        with tarfile.open(fileobj=io.BytesIO(artifacts.values["result"])) as archive:
            assert (
                archive.extractfile("result.txt").read()
                == b"Prepared through the Benchmark SDK\nDriven through the Benchmark SDK\n"
            )
        native_task = await asyncio.to_thread(
            runtime.find, compiled.scope, compiled.leases["task"][1].handle
        )
        assert (await asyncio.to_thread(runtime.inspect, native_task))["status"] == "running"
        async with AsyncSession(_engine) as db:
            sessions = (
                await db.scalars(
                    select(BenchmarkEnvironmentSession).options(
                        undefer(BenchmarkEnvironmentSession.private_plan)
                    )
                )
            ).all()
            assert len(sessions) == 2
            assert all(b"alpha-private-fixture" not in session.private_plan for session in sessions)
            operations = (await db.scalars(select(BenchmarkOperation))).all()
            assert all(operation.state == "complete" for operation in operations)
            assert (
                len(
                    [
                        operation
                        for operation in operations
                        if operation.actor_id == worker_host.worker_actor
                    ]
                )
                == 4
            )
        await store.transition(owner, "capturing", "cleaning")
        await channel.invoke("cleanup", scoped)
        await channel.close(grace=1)
        assert (await asyncio.to_thread(runtime.inspect, native_task))["status"] == "running"
        recovery = RecoveryService(
            store,
            worker_host.host_actor,
            {
                "fixture.rootless": lambda authority: RootlessReconciler(
                    runtime, authority, "fixture.rootless"
                )
            },
        )
        released = await recovery.sweep(owner, "release")
        assert len(released) == 4 and all(value["state"] == "released" for value in released)
        await store.transition(owner, "cleaning", "finished")
        async with AsyncSession(_engine) as db:
            finished = await db.get(BenchmarkTrial, owner.trial_id)
            assert (
                finished.execution_status == "success" and finished.recording_status == "complete"
            )
            assert finished.cleanup_status == "complete"
            assert all(
                lease.state == "released"
                for lease in (await db.scalars(select(BenchmarkResourceLease))).all()
            )
            public = [
                entry.public_data
                for entry in (await db.scalars(select(BenchmarkJournalEntry))).all()
            ]
            assert "alpha-private-fixture" not in json.dumps(public)
    finally:
        # Assertion failures must still remove only this fixture's exact scopes.
        candidates = []
        for selected_host in hosts:
            candidates += [
                (
                    selected_host.scope,
                    str(
                        uuid.uuid5(
                            uuid.UUID(selected_host.scope.session_id),
                            "resource:" + selected_host.WORKER_ID,
                        )
                    ),
                )
            ]
            if selected_host.environment is not None:
                candidates += [
                    (selected_host.environment.scope, lease.handle)
                    for _, lease in selected_host.environment.leases.values()
                    if lease.kind == "container"
                ]
        for selected_scope, handle in candidates:
            with suppress(Exception):
                native = await asyncio.to_thread(runtime.find, selected_scope, handle)
                if native is not None:
                    await asyncio.to_thread(runtime.stop, native, grace_seconds=0, remove=True)
        for selected_host in hosts:
            if selected_host.channel is not None:
                with suppress(Exception):
                    await selected_host.channel.close(grace=0.2)
            await selected_host.host_broker.close()
            await selected_host.worker_broker.close()
        if source_alias is not None:
            with suppress(Exception):
                await asyncio.to_thread(docker_client.images.remove, source_alias)
        docker_client.close()
