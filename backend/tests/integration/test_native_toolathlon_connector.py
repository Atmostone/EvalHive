"""The pinned native Toolathlon world runs through both real harnesses offline."""

import asyncio
import io
import json
import os
import tarfile
import uuid
from contextlib import suppress
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import docker
import pytest
import pytest_asyncio
import urllib3
from app.benchmarks.composition import RootlessCoordinator
from app.benchmarks.driver import DriverLimits, TrialDriver
from app.benchmarks.evaluation_controls import EvaluationControls
from app.benchmarks.evidence import EvidenceStore
from app.benchmarks.private_store import PrivateStore
from app.benchmarks.trials import TrialStore
from app.harnesses.catalog import get_manifest
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_trial import BenchmarkResourceLease, BenchmarkTrial
from app.models.execution_attempt import ExecutionAttempt, ExecutionEvent
from app.models.task import Task
from app.runtime.bundles import install_bundle
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.managed_docker import RootlessDockerRuntime
from app.runtime.managed_spec import PathMapping, RuntimePolicy
from app.runtime.managed_storage import RootlessVolumeRuntime
from app.runtime.registered_assets import RegisteredFile, RootlessAssets
from app.runtime.registered_files import load_registered_assets
from evalhive_runner.runner import RunnerPolicy
from minio import Minio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tests.integration.test_benchmark_worker_host_native import WORKSPACE
from tests.runtime.agent_worker_fixture import SERVER as BASH_SERVER
from tests.runtime.authority_fixture import FixtureArtifacts, manifest

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_TOOLATHLON") != "1",
    reason="Requires explicitly built pinned Toolathlon toolchain",
)


@pytest_asyncio.fixture
async def native_engine(_engine, db_session):
    pooled = create_async_engine(_engine.url, pool_size=5, max_overflow=5)
    try:
        yield pooled
    finally:
        await pooled.dispose()


@pytest.mark.parametrize("connector_id", ["pi", "opencode"])
async def test_native_toolathlon_world_runs_and_grades_through_real_harness(
    native_engine, connector_id
):
    # Native imports are confined to the engineering test process.
    from evalhive_toolathlon.dataset import InstalledToolathlon
    from evalhive_toolathlon.worker import MANIFEST

    engine = native_engine
    local = Path(os.environ["EVALHIVE_NATIVE_FIXTURE_DIR"]) / ("toolathlon-" + connector_id)
    host_root = os.environ["EVALHIVE_NATIVE_FIXTURE_HOST"] + "/toolathlon-" + connector_id
    local.mkdir(parents=True)
    bundle_root = (
        Path(os.getenv("EVALHIVE_NATIVE_BUNDLE_DIR", str(local / "bundle"))) / connector_id
    )
    bundle_host = (
        os.getenv("EVALHIVE_NATIVE_BUNDLE_HOST", host_root + "/bundle") + "/" + connector_id
    )
    bundle_root.mkdir(parents=True, exist_ok=True)
    installed = Path(os.environ["EVALHIVE_TOOLATHLON_INSTALL"])
    installed_host = os.environ["EVALHIVE_TOOLATHLON_INSTALL_HOST"]
    dataset = InstalledToolathlon(installed)
    receipt = dataset.receipt
    descriptor = next(
        value
        for value in receipt["cases"]
        if value["ref"]["case_id"] == "terminal-pw-howtocook-excel-word-gcal"
    )
    client = docker.DockerClient(base_url="unix:///run/user/11001/docker.sock", timeout=15)
    runtime = RootlessDockerRuntime(
        client,
        RuntimePolicy(
            PathMapping(host_root, str(local)),
            (
                PathMapping(bundle_host, str(bundle_root)),
                PathMapping(installed_host, str(installed)),
            ),
        ),
    )
    declaration = {**deepcopy(MANIFEST), "image": receipt["worker_image"]}
    await asyncio.to_thread(runtime.resolve_image, declaration["image"])
    await asyncio.to_thread(
        runtime.resolve_image, os.environ["EVALHIVE_TOOLATHLON_ENVIRONMENT_IMAGE"]
    )
    harness = get_manifest(connector_id)
    installation = deepcopy(harness)
    installation["requirements"]["runtime_bundle"]["image"] = (
        "evalhive-" + connector_id + "-bundle:development"
    )
    bundle = await asyncio.to_thread(
        install_bundle,
        runtime,
        installation,
        PathMapping(bundle_host, str(bundle_root)),
    )
    provider_id = "evalhive.rootless"
    provider_manifest = {**manifest(), "id": provider_id}
    installed_assets = load_registered_assets(
        PathMapping(installed_host, str(installed)),
        runtime,
        declaration,
        bindings={
            "harness.runtime": RegisteredFile(bundle.host_path, "sha256:" + bundle.fingerprint)
        },
    )
    registration = deepcopy(installed_assets.document)
    # This local endpoint is an explicit engineering fixture, not a native model.
    server = (
        BASH_SERVER.replace(
            "if first and not any(tool['function']['name']=='bash' for tool in body.get('tools',[])):",
            "selected=next((tool['function']['name'] for tool in body.get('tools',[]) if tool['function']['name'].endswith('run_command')),None)\n   if first and not selected:",
        )
        .replace("'name':'bash'", "'name':selected")
        .replace(",'description':'Verify native task dependencies'", "")
    )
    registration["assets"]["toolathlon-gym.task"]["command"] = [
        "/opt/venv/bin/python",
        "-c",
        "from pathlib import Path;import time;assert not Path('/gym').exists();assert not Path('/dataset').exists();Path('/workspace/result.txt').write_text('Prepared through native Toolathlon\\n');exec("
        + repr(server)
        + ");time.sleep(900)",
    ]
    assets = RootlessAssets(
        registration,
        runtime,
        files=installed_assets.files,
        connector_id=declaration["id"],
        connector_version=declaration["version"],
    )
    async with AsyncSession(engine) as db:
        task = Task(
            title="Native Toolathlon engineering acceptance",
            workspace_id=WORKSPACE,
            status="in_progress",
        )
        db.add(task)
        await db.flush()
        task_id = task.id
        await db.commit()
    store = TrialStore(engine, PrivateStore("native-toolathlon-fixture-key"))
    trial = await store.create(
        WORKSPACE,
        task_id=task_id,
        snapshot={"fixture": "native Toolathlon with local fake endpoint"},
        deadline_at=datetime.now(UTC) + timedelta(seconds=600),
    )
    owner = await store.claim(WORKSPACE, trial.id, uuid.uuid4())

    evidence_client = Minio(
        os.environ["EVALHIVE_EVIDENCE_MINIO"],
        access_key="fixture-evidence",
        secret_key="fixture-evidence-password",
        secure=False,
        http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=3, read=10), retries=False),
    )
    bucket = "toolathlon-engineering-" + uuid.uuid4().hex
    await asyncio.to_thread(evidence_client.make_bucket, bucket)
    evidence = EvidenceStore(store, evidence_client, bucket=bucket)

    class Capture(FixtureArtifacts):
        def __init__(self, selected=evidence, selected_owner=owner):
            super().__init__()
            self.evidence, self.owner = selected, selected_owner

        async def save(self, scope, grant, chunks, *, publication=None):
            value = bytearray()

            async def observed():
                async for chunk in chunks:
                    value.extend(chunk)
                    yield chunk

            saved = await self.evidence.sink(self.owner).save(
                scope, grant, observed(), publication=publication
            )
            self.values[grant.artifact_id] = bytes(value)
            return saved

    artifacts = Capture()

    class EngineeringProvider(RootlessDockerProvider):
        async def _dispatch(self, call, request):
            try:
                response = await super()._dispatch(call, request)
            except Exception as error:
                with (local / "engineering-provider-errors.jsonl").open("a") as record:
                    record.write(
                        json.dumps(
                            {
                                "operation": request["operation"],
                                "request_id": request["id"],
                                "exception": type(error).__name__,
                                "message": str(error),
                            },
                        )
                        + "\n"
                    )
                raise
            if request["operation"] == "exec" and request["payload"].get("mode", "wait") == "wait":
                with (local / "engineering-native-commands.jsonl").open("a") as record:
                    record.write(
                        json.dumps(
                            {
                                "request_id": request["id"],
                                "argv": request["payload"]["argv"],
                                **{
                                    key: response.get(key)
                                    for key in ("exit_code", "stdout", "stderr")
                                },
                            }
                        )
                        + "\n"
                    )
            return response

    class EngineeringCoordinator(RootlessCoordinator):
        def _driver(self, selected_host, selected_factories, driver_class):
            driver = super()._driver(selected_host, selected_factories, driver_class)
            pipeline = driver._pipeline

            async def observed_pipeline(*args, **options):
                try:
                    return await pipeline(*args, **options)
                except Exception as error:
                    with (local / "engineering-controller-errors.jsonl").open("a") as record:
                        record.write(
                            json.dumps(
                                {
                                    "purpose": selected_host.purpose,
                                    "exception": type(error).__name__,
                                    "code": getattr(error, "code", None),
                                    "kind": getattr(error, "kind", None),
                                    "message": str(error),
                                }
                            )
                            + "\n"
                        )
                    raise

            driver._pipeline = observed_pipeline
            return driver

        def _providers(self, selected_store, selected_owner):
            super()._providers(selected_store, selected_owner)
            return {
                provider_id: lambda authority: EngineeringProvider(
                    runtime,
                    authority,
                    provider_manifest,
                    artifacts=self.evidence.sink(selected_owner),
                )
            }

    credential = uuid.uuid4().hex
    harness_spec = {
        "protocol_version": "1.0",
        "workspace_id": str(WORKSPACE),
        "task_id": str(task_id),
        "attempt_id": str(uuid.uuid4()),
        "task": {"prompt": "Frozen by prepared access"},
        "cwd": "/workspace",
        "config": {},
        "limits": {"max_turns": 4},
        "model": {
            "name": "fake-model",
            "endpoint": "http://127.0.0.1:8765/v1",
            "credential_ref": "model_api_key",
        },
    }
    coordinator = EngineeringCoordinator(
        runtime,
        provider_manifest,
        SimpleNamespace(
            store=store, sink=lambda selected: artifacts, validate_capture=evidence.validate_capture,
            open_verified=evidence.open_verified,
        ),
        limits=DriverLimits(
            phase_seconds=300, capture_seconds=120, cleanup_seconds=60, service_seconds=15
        ),
        runner_policy=RunnerPolicy(heartbeat_seconds=0.2, startup_seconds=60, command_seconds=60),
    )
    composed = coordinator.execution(
        store,
        owner,
        declaration,
        assets,
        harness_manifest=harness,
        harness_spec=harness_spec,
        bundle=bundle,
        credentials={"model_api_key": credential},
        spool_root=local / "spool",
    )
    host = composed.host

    class EngineeringDriver(TrialDriver):
        async def _capture(self, channel):
            if self.host.environment is not None and "task" in self.host.environment.leases:
                task_lease = self.host.environment.leases["task"][1]
                try:
                    observation = await self.host.host_broker.handle(
                        self.host.request(
                            self.host.scope,
                            "exec",
                            handle=task_lease.handle,
                            request_id="engineering.resources",
                            payload={
                                "argv": [
                                    "/opt/venv/bin/python",
                                    "-c",
                                    "from pathlib import Path;import json;root=Path('/sys/fs/cgroup');print(json.dumps({name:(root/name).read_text() for name in ['memory.events','memory.peak','cpu.stat'] if (root/name).is_file()}))",
                                ],
                                "timeout_seconds": 10,
                                "deadline_policy": "inherit",
                            },
                        )
                    )
                except Exception as error:
                    observation = {
                        "exception": type(error).__name__,
                        "code": getattr(error, "code", None),
                    }
                (local / "engineering-resource-observation.json").write_text(
                    json.dumps(observation, indent=2) + "\n"
                )
            return await super()._capture(channel)

        async def _pipeline(self, *args, **options):
            try:
                return await super()._pipeline(*args, **options)
            except Exception as error:
                (local / "engineering-controller-error.json").write_text(
                    json.dumps(
                        {
                            "exception": type(error).__name__,
                            "code": getattr(error, "code", None),
                            "kind": getattr(error, "kind", None),
                            "message": str(error),
                        },
                        indent=2,
                    )
                    + "\n"
                )
                raise

    controller = EngineeringDriver(
        host,
        composed.recovery,
        capture_providers=composed.capture_providers,
        capture_validator=evidence.validate_capture,
        deadlines=composed.deadlines,
        limits=composed.limits,
    )
    run_spec = {
        "protocol_version": "1.0",
        "scope": {
            "workspace_id": str(WORKSPACE),
            "trial_id": str(trial.id),
            "generation": owner.generation,
        },
        "case": descriptor,
        "config": {
            "prepare_timeout_seconds": 240,
            "command_timeout_seconds": 120,
            "evaluation_timeout_seconds": 120,
        },
        "credential_refs": {},
        "models": {},
        "limits": {"wall_time_seconds": 600, "memory_mb": 3072, "cpu_cores": 1.5, "disk_mb": 2048},
    }
    hosts = [host]
    try:
        result = await asyncio.wait_for(controller.run(run_spec), 580)
        (local / "engineering-result.json").write_text(json.dumps(result, indent=2) + "\n")
        async with AsyncSession(engine) as db:
            attempt = await db.get(ExecutionAttempt, host.agent_gateway.calls.session_id)
            if attempt is not None:
                events = list(
                    await db.scalars(
                        select(ExecutionEvent.payload).where(
                            ExecutionEvent.attempt_id == attempt.id
                        )
                    )
                )
                (local / "engineering-harness-events.json").write_text(
                    json.dumps(
                        {
                            "status": attempt.status,
                            "result": attempt.result,
                            "events": events,
                        },
                        indent=2,
                    )
                    + "\n"
                )
        assert result["phase"] == "finished" and result["cleanup"] == "complete", result
        assert result["execution"] == "success" and result["evaluation"] == "completed", result
        with tarfile.open(fileobj=io.BytesIO(artifacts.values["workspace"])) as archive:
            assert archive.extractfile("workspace/agent-child.started").read() == b"started"
            assert (
                b"Driven through the Benchmark SDK"
                in archive.extractfile("workspace/result.txt").read()
            )
        async with AsyncSession(engine) as db:
            evaluation = await db.scalar(
                select(BenchmarkEvaluationAttempt).where(
                    BenchmarkEvaluationAttempt.trial_id == trial.id
                )
            )
            assert evaluation.status == "completed" and evaluation.success is False
            attempt = await db.get(ExecutionAttempt, host.agent_gateway.calls.session_id)
            assert attempt.connector_id == connector_id and attempt.status == "success"
            assert "/gym/tasks" not in json.dumps(attempt.run_spec)
            assert "/workspace" in json.dumps(attempt.run_spec["instructions"])
            events = list(
                await db.scalars(
                    select(ExecutionEvent.payload).where(ExecutionEvent.attempt_id == attempt.id)
                )
            )
            started_calls = {
                (event.get("session_id"), event["data"]["call_id"])
                for event in events
                if event["event"] == "tool.start" and event["data"]["name"].endswith("run_command")
            }
            completed_calls = {
                (event.get("session_id"), event["data"]["call_id"])
                for event in events
                if event["event"] == "tool.end" and event["data"]["status"] == "completed"
            }
            assert started_calls and started_calls <= completed_calls
            assert credential not in json.dumps(events)
            leases = list(
                await db.scalars(
                    select(BenchmarkResourceLease).where(
                        BenchmarkResourceLease.trial_id == trial.id
                    )
                )
            )
            assert leases and all(lease.state == "released" for lease in leases)
            source_id, source_digest = evaluation.id, evaluation.result_digest
            source_sequence = (await db.get(BenchmarkTrial, trial.id)).next_seq
        if connector_id == "pi":
            grading = EvaluationControls(store, evidence)
            grading_owner = await grading.create(
                WORKSPACE,
                trial.id,
                source_id,
                uuid.uuid4(),
                deadline_at=datetime.now(UTC) + timedelta(seconds=600),
            )
            grading_evidence = EvidenceStore(grading, evidence_client, bucket=bucket)
            artifacts = Capture(grading_evidence, grading_owner)
            grading_coordinator = EngineeringCoordinator(
                runtime,
                provider_manifest,
                SimpleNamespace(
                    store=grading,
                    sink=lambda selected: artifacts,
                    validate_capture=grading_evidence.validate_capture,
                    open_verified=grading_evidence.open_verified,
                ),
                limits=controller.limits,
            )
            evaluator = await grading_coordinator.evaluation(grading, grading_owner)
            fresh = evaluator.host
            hosts.append(fresh)
            restored = await evaluator.run()
            (local / "engineering-regrade-result.json").write_text(
                json.dumps(restored, indent=2) + "\n"
            )
            assert (
                restored["phase"] == "finished"
                and restored["evaluation"] == "completed"
                and restored["cleanup"] == "complete"
            ), restored
            assert fresh.agent_gateway is None and "task" not in fresh.environment.leases
            assert fresh.environment.scope.session_id != host.environment.scope.session_id
            async with AsyncSession(engine) as db:
                repeated = await db.get(BenchmarkEvaluationAttempt, grading_owner.control_id)
                assert repeated.success is False and repeated.source_evaluation_id == source_id
                assert (
                    await db.get(BenchmarkEvaluationAttempt, source_id)
                ).result_digest == source_digest
                assert (await db.get(BenchmarkTrial, trial.id)).next_seq == source_sequence
    finally:
        for created in reversed(hosts):
            scopes = [
                (
                    created.scope,
                    str(
                        uuid.uuid5(
                            uuid.UUID(created.scope.session_id), "resource:" + created.WORKER_ID
                        )
                    ),
                )
            ]
            if created.environment is not None:
                scopes.extend(
                    (created.environment.scope, lease.handle)
                    for _, lease in created.environment.leases.values()
                    if lease.kind == "container"
                )
            for scope, handle in scopes:
                with suppress(Exception):
                    native = await asyncio.to_thread(runtime.find, scope, handle)
                    if native is not None:
                        await asyncio.to_thread(runtime.stop, native, grace_seconds=0, remove=True)
            if created.environment is not None:
                storage = RootlessVolumeRuntime(runtime)
                for _, lease in reversed(list(created.environment.leases.values())):
                    if lease.kind not in {"volume", "network"}:
                        continue
                    with suppress(Exception):
                        lookup = storage.find if lease.kind == "volume" else runtime.find_network
                        remove = (
                            storage.remove if lease.kind == "volume" else runtime.remove_network
                        )
                        native = await asyncio.to_thread(
                            lookup, created.environment.scope, lease.handle
                        )
                        if native is not None:
                            await asyncio.to_thread(remove, native)
            if created.channel is not None:
                with suppress(Exception):
                    await created.channel.close(grace=0.2)
            await created.host_broker.close()
            await created.worker_broker.close()
        client.close()
