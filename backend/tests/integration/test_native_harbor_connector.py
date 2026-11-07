"""Native Harbor checks and artifact replay through the real SDK composition."""

import asyncio
import io
import json
import os
import shlex
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
from app.benchmarks.driver import DriverLimits
from app.benchmarks.evaluation_controls import EvaluationControls
from app.benchmarks.evidence import EvidenceStore
from app.benchmarks.private_store import PrivateStore
from app.benchmarks.trials import TrialStore
from app.harnesses.catalog import get_manifest
from app.models.benchmark_command import BenchmarkCommand
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_trial import (
    BenchmarkJournalEntry,
    BenchmarkOperation,
    BenchmarkResourceLease,
    BenchmarkTrial,
)
from app.models.execution_attempt import ExecutionAttempt, ExecutionEvent
from app.models.task import Task
from app.runtime.bundles import install_bundle
from app.runtime.managed_docker import RootlessDockerRuntime
from app.runtime.managed_spec import PathMapping, RuntimePolicy
from app.runtime.managed_storage import RootlessVolumeRuntime
from app.runtime.registered_assets import RegisteredFile
from app.runtime.registered_files import load_registered_assets
from evalhive_runner.runner import RunnerPolicy
from minio import Minio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import undefer

from tests.fixtures.native_model_endpoint import NativeModelEndpoint
from tests.integration.test_benchmark_worker_host_native import WORKSPACE
from tests.runtime.authority_fixture import FixtureArtifacts, manifest

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_HARBOR") != "1",
    reason="Requires explicitly built pinned Harbor and native fixture images",
)

TOOL_SCRIPT = r"""
import os,time
from pathlib import Path
assert os.getuid()==1000
assert not Path('/dataset').exists()
assert not Path('/tests').exists()
assert not Path('/evalhive-stage').joinpath('private-gold').exists()
os.umask(0o077)
Path('/workspace/result.txt').write_text('Driven through the Benchmark SDK\n')
Path('/logs/artifacts/answer.txt').write_text('Native artifact\n')
if os.fork()==0:
 os.setsid()
 descriptor=os.open('/dev/null',os.O_RDWR)
 for target in (0,1,2): os.dup2(descriptor,target)
 from http.server import BaseHTTPRequestHandler,HTTPServer
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*args): pass
  def do_GET(self):
   self.send_response(200);self.end_headers();self.wfile.write(b'Owned native service')
 Path('/workspace/agent-child.started').write_text('started')
 HTTPServer(('127.0.0.1',8767),Handler).serve_forever()
"""


@pytest_asyncio.fixture
async def native_engine(_engine, db_session):
    pooled = create_async_engine(_engine.url, pool_size=5, max_overflow=5)
    try:
        yield pooled
    finally:
        await pooled.dispose()


@pytest.mark.parametrize(
    "connector_id,mode",
    [("pi", "shared"), ("opencode", "shared"), ("pi", "separate"), ("opencode", "separate")],
)
async def test_harbor_native_verification_and_replay(native_engine, connector_id, mode):
    # Native imports are confined to this engineering controller.
    from evalhive_harbor.images import inspect_image
    from evalhive_harbor.installation import install
    from evalhive_harbor.worker import MANIFEST

    engine = native_engine
    services_enabled = os.getenv("EVALHIVE_NATIVE_HARBOR_SERVICES") == "1"
    local = Path(os.environ["EVALHIVE_NATIVE_FIXTURE_DIR"]) / (
        "harbor-" + connector_id + "-" + mode
    )
    host_root = os.environ["EVALHIVE_NATIVE_FIXTURE_HOST"] + "/" + local.name
    local.mkdir(parents=True)
    source = local / "source"
    native = source / "native-engineering"
    (native / "environment").mkdir(parents=True)
    (native / "tests").mkdir()
    (native / "environment/Dockerfile").write_text(
        (Path(__file__).parents[1] / "fixtures/harbor_task/Dockerfile").read_text()
    )
    (native / "instruction.md").write_text("Run the requested native shell check and finish.\n")
    config = (
        'version="1.0"\nartifacts=["/workspace/result.txt"]\n[environment]\ncpus=0.5\nmemory_mb=512\nstorage_mb=64\nnetwork_mode="no-network"\n[agent]\nuser="1000"\ntimeout_sec=60\n[verifier]\ntimeout_sec=20\nenvironment_mode="'
        + mode
        + '"\n'
    )
    if mode == "separate":
        config += '[verifier.environment]\ncpus=0.25\nmemory_mb=128\nstorage_mb=64\nnetwork_mode="no-network"\n'
    else:
        config += 'user="1000"\n'
    if services_enabled:
        config = config.replace('network_mode="no-network"', 'network_mode="public"', 1)
        config = config.replace(
            'artifacts=["/workspace/result.txt"]',
            'artifacts=["/workspace/result.txt",{source="/workspace/sidecar.txt",service="database"}]',
        )
        health = shlex.join(
            [
                "python",
                "-c",
                "from urllib.request import urlopen; assert urlopen('http://database:8768/sidecar.txt',timeout=2).read()==b'Native sidecar artifact\\n'",
            ]
        )
        config += (
            "\n[environment.healthcheck]\ncommand="
            + json.dumps(health)
            + "\ntimeout_sec=10\ninterval_sec=0.2\n"
        )
        database = native / "environment/database"
        database.mkdir()
        (database / "Dockerfile").write_text(
            (Path(__file__).parents[1] / "fixtures/harbor_task/Dockerfile.service").read_text()
        )
        server = "import os\nfrom pathlib import Path\nassert os.getuid()==1000\nos.umask(0o077)\nPath('/workspace/sidecar.txt').write_text('Native sidecar artifact\\n')\nfrom http.server import HTTPServer,SimpleHTTPRequestHandler\nHTTPServer(('0.0.0.0',8768),SimpleHTTPRequestHandler).serve_forever()\n"
        (native / "environment/docker-compose.yaml").write_text(
            json.dumps(
                {
                    "services": {
                        "main": {"depends_on": ["database"], "volumes": ["shared:/workspace"]},
                        "database": {
                            "build": "database",
                            "command": ["python", "-c", server],
                            "user": "1000",
                            "cpus": 0.25,
                            "mem_limit": "128m",
                            "volumes": ["shared:/workspace"],
                        },
                    },
                    "volumes": {"shared": {}},
                },
                indent=2,
            )
        )
    (native / "task.toml").write_text(config)
    checker = "import os,json\nfrom pathlib import Path\nassert os.getuid()==0\nassert 'Driven through the Benchmark SDK' in Path('/workspace/result.txt').read_text()\nassert Path('/logs/artifacts/answer.txt').read_text()=='Native artifact\\n'\n"
    if mode == "shared":
        checker = checker.replace("os.getuid()==0", "os.getuid()==1000")
        checker += "from urllib.request import urlopen\nassert urlopen('http://127.0.0.1:8767',timeout=2).read()==b'Owned native service'\n"
    if services_enabled:
        checker += (
            "assert Path('/workspace/sidecar.txt').read_text()=='Native sidecar artifact\\n'\n"
        )
        if mode == "shared":
            checker += "assert urlopen('http://database:8768/sidecar.txt',timeout=2).read()==b'Native sidecar artifact\\n'\n"
    checker += "Path('/logs/verifier/reward.json').write_text(json.dumps({'quality':0.625}))\n"
    (native / "tests/test.sh").write_text("#!/bin/bash\npython - <<'PY'\n" + checker + "PY\n")
    client = docker.DockerClient(base_url="unix:///run/user/11001/docker.sock", timeout=20)
    worker_image = client.images.get("evalhive-benchmark-harbor:development")
    worker_id = worker_image.id
    worker_image.tag(
        "evalhive-benchmark-harbor", "retained-" + worker_id.removeprefix("sha256:")[:12]
    )
    task_image = client.images.get("evalhive-harbor-task:engineering")
    task_id = task_image.id
    task_image.tag("evalhive-harbor-task", "retained-" + task_id.removeprefix("sha256:")[:12])
    tools = local / "transfer-tools"
    tools.mkdir()
    exported = client.containers.create(
        worker_id, labels={"evalhive.engineering": "harbor-transfer-export"}
    )
    try:
        stream, _ = exported.get_archive("/evalhive-transfer")
        with tarfile.open(fileobj=io.BytesIO(b"".join(stream)), mode="r:") as archive:
            for name in ("busybox", "ld-musl-x86_64.so.1"):
                (tools / name).write_bytes(archive.extractfile("evalhive-transfer/" + name).read())
                (tools / name).chmod(0o555)
    finally:
        exported.remove(v=True)
    images = {
        "task": inspect_image(
            client, task_id, limits={"memory_mb": 512, "cpu_cores": 0.5, "disk_mb": 64}
        )
    }
    if mode == "separate":
        images["verifier"] = inspect_image(
            client, task_id, limits={"memory_mb": 128, "cpu_cores": 0.25, "disk_mb": 64}
        )
    if services_enabled:
        service_image = client.images.get("evalhive-harbor-service:engineering")
        service_image.tag(
            "evalhive-harbor-service", "retained-" + service_image.id.removeprefix("sha256:")[:12]
        )
        images["services"] = {
            "task": {
                "database": inspect_image(
                    client,
                    service_image.id,
                    limits={"memory_mb": 128, "cpu_cores": 0.25, "disk_mb": 64},
                )
            }
        }
        images["volumes"] = {"task": {"shared": 64}}
    installed = local / "installed"
    receipt = install(
        source,
        installed,
        revision="native-services-2" if services_enabled else "native-engineering-1",
        worker_image=worker_id,
        image_bindings={"native-engineering": images},
        transfer_tools=tools,
        dataset_id="harbor-services-engineering" if services_enabled else "harbor-engineering",
    )
    installed_host = host_root + "/installed"
    bundle_root = Path(os.environ["EVALHIVE_NATIVE_BUNDLE_DIR"]) / connector_id
    bundle_host = os.environ["EVALHIVE_NATIVE_BUNDLE_HOST"] + "/" + connector_id
    bundle_root.mkdir(parents=True, exist_ok=True)
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
    harness = get_manifest(connector_id)
    bundle_manifest = deepcopy(harness)
    bundle_manifest["requirements"]["runtime_bundle"]["image"] = (
        "evalhive-" + connector_id + "-bundle:development"
    )
    bundle = await asyncio.to_thread(
        install_bundle, runtime, bundle_manifest, PathMapping(bundle_host, str(bundle_root))
    )
    declaration = {**deepcopy(MANIFEST), "image": worker_id}
    assets = load_registered_assets(
        PathMapping(installed_host, str(installed)),
        runtime,
        declaration,
        bindings={
            "harness.runtime": RegisteredFile(bundle.host_path, "sha256:" + bundle.fingerprint)
        },
    )
    async with AsyncSession(engine) as db:
        task = Task(
            title="Native Harbor engineering acceptance",
            workspace_id=WORKSPACE,
            status="in_progress",
        )
        db.add(task)
        await db.flush()
        task_ref = task.id
        await db.commit()
    store = TrialStore(engine, PrivateStore("native-harbor-fixture-key"))
    trial = await store.create(
        WORKSPACE,
        task_id=task_ref,
        snapshot={"fixture": "native Harbor", "mode": mode},
        deadline_at=datetime.now(UTC) + timedelta(seconds=240),
    )
    owner = await store.claim(WORKSPACE, trial.id, uuid.uuid4())
    objects = Minio(
        os.environ["EVALHIVE_EVIDENCE_MINIO"],
        access_key="fixture-evidence",
        secret_key="fixture-evidence-password",
        secure=False,
        http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=3, read=10), retries=False),
    )
    bucket = "harbor-engineering-" + uuid.uuid4().hex
    await asyncio.to_thread(objects.make_bucket, bucket)
    evidence = EvidenceStore(store, objects, bucket=bucket)

    class Capture(FixtureArtifacts):
        def __init__(self, selected_owner, selected_evidence=evidence):
            super().__init__()
            self.owner = selected_owner
            self.evidence = selected_evidence

        async def save(self, scope, grant, chunks, *, publication=None):
            value = bytearray()

            async def observed():
                async for chunk in chunks:
                    value.extend(chunk)
                    yield chunk

            saved = await self.evidence.sink(self.owner).save(
                scope, grant, observed(), publication=publication
            )
            destination = local / (
                "engineering-evidence-"
                + scope.session_id
                + "-"
                + str(len(self.values))
                + "-"
                + grant.artifact_id
                + ".tar"
            )
            with os.fdopen(
                os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
            ) as stream:
                stream.write(value)
            self.values[grant.artifact_id] = bytes(value)
            return saved

    artifacts = Capture(owner)
    facade = SimpleNamespace(
        store=store,
        sink=lambda selected: artifacts,
        validate_capture=evidence.validate_capture,
        open_verified=evidence.open_verified,
    )
    provider = {**manifest(), "id": "evalhive.rootless"}
    coordinator = RootlessCoordinator(
        runtime,
        provider,
        facade,
        limits=DriverLimits(
            phase_seconds=120, capture_seconds=60, cleanup_seconds=45, service_seconds=15
        ),
        runner_policy=RunnerPolicy(heartbeat_seconds=0.2, startup_seconds=30, command_seconds=30),
    )
    hosts = []
    credential = uuid.uuid4().hex

    async def save_operation_diagnostics():
        async with AsyncSession(engine) as db:
            operations = list(
                await db.scalars(
                    select(BenchmarkOperation)
                    .where(BenchmarkOperation.trial_id == trial.id)
                    .options(
                        undefer(BenchmarkOperation.private_request),
                        undefer(BenchmarkOperation.private_result),
                    )
                    .order_by(BenchmarkOperation.started_at)
                )
            )
            diagnostic = []
            for operation in operations:
                request = store.private.open(
                    "operation-request:" + str(operation.id), operation.private_request
                )
                response = (
                    store.private.open(
                        "operation-result:" + str(operation.id), operation.private_result
                    ).get("result", {})
                    if operation.private_result
                    else {}
                )
                diagnostic.append(
                    {
                        "request_id": operation.request_id,
                        "operation": request.get("operation"),
                        "handle": request.get("handle"),
                        "state": operation.state,
                        "error_code": operation.error_code,
                        "started_at": operation.started_at.isoformat(),
                        "seconds": (operation.finished_at - operation.started_at).total_seconds()
                        if operation.finished_at
                        else None,
                        "exit_code": response.get("exit_code"),
                        "native_state": {
                            key: response["native"][key]
                            for key in ("status", "health", "exit_code")
                            if key in response.get("native", {})
                        },
                        "stderr": str(response.get("stderr", "")).replace(credential, "[redacted]")[
                            :512
                        ],
                    }
                )
            journal = list(
                await db.scalars(
                    select(BenchmarkJournalEntry)
                    .where(BenchmarkJournalEntry.trial_id == trial.id)
                    .order_by(BenchmarkJournalEntry.seq)
                )
            )
            leases = list(
                await db.scalars(
                    select(BenchmarkResourceLease).where(
                        BenchmarkResourceLease.trial_id == trial.id
                    )
                )
            )
            (local / "engineering-operation-diagnostics.json").write_text(
                json.dumps(
                    {
                        "operations": diagnostic,
                        "journal": [
                            {
                                "event": entry.event,
                                "public": entry.public_data,
                                "at": entry.created_at.isoformat(),
                            }
                            for entry in journal
                        ],
                        "leases": [
                            {
                                "handle": str(lease.id),
                                "logical_id": lease.logical_id,
                                "kind": lease.kind,
                                "state": lease.state,
                            }
                            for lease in leases
                        ],
                    },
                    indent=2,
                )
            )

    with NativeModelEndpoint(credential, tool_script=TOOL_SCRIPT) as endpoint:
        harness_spec = {
            "protocol_version": "1.0",
            "workspace_id": str(WORKSPACE),
            "task_id": str(task_ref),
            "attempt_id": str(uuid.uuid4()),
            "task": {"prompt": "Bound by native prepared access"},
            "cwd": "/workspace",
            "config": {},
            "limits": {"max_turns": 4},
            "model": {
                "name": "fake-model",
                "endpoint": endpoint.url,
                "credential_ref": "model_api_key",
            },
        }
        controller = coordinator.execution(
            store,
            owner,
            declaration,
            assets,
            harness_manifest=harness,
            harness_spec=harness_spec,
            bundle=bundle,
            credentials={"model_api_key": credential},
            spool_root=local / "spool",
            model_gateway=True,
        )
        hosts.append(controller.host)
        original_pipeline = controller._pipeline

        async def observed_pipeline(*args, **options):
            try:
                return await original_pipeline(*args, **options)
            except Exception as error:
                async with AsyncSession(engine) as diagnostic_db:
                    commands = list(
                        await diagnostic_db.scalars(
                            select(BenchmarkCommand)
                            .where(BenchmarkCommand.trial_id == trial.id)
                            .options(undefer(BenchmarkCommand.private_response))
                        )
                    )
                    diagnostic = []
                    for command in commands:
                        receipt = (
                            store.private.open(
                                "command-response:" + str(command.id), command.private_response
                            )
                            if command.private_response
                            else {}
                        )
                        diagnostic.append(
                            {
                                "method": command.method,
                                "phase": command.phase,
                                "state": command.state,
                                "error_code": command.error_code,
                                "response_error": receipt.get("error"),
                            }
                        )
                    (local / "engineering-command-diagnostics.json").write_text(
                        json.dumps(diagnostic, indent=2)
                    )
                (local / "engineering-controller-error.json").write_text(
                    json.dumps(
                        {
                            "exception": type(error).__name__,
                            "code": getattr(error, "code", None),
                            "message": str(error),
                        },
                        indent=2,
                    )
                )
                raise

        controller._pipeline = observed_pipeline
        run_spec = {
            "protocol_version": "1.0",
            "scope": {
                "workspace_id": str(WORKSPACE),
                "trial_id": str(trial.id),
                "generation": owner.generation,
            },
            "case": receipt["cases"][0],
            "config": {},
            "credential_refs": {},
            "models": {},
            "limits": {
                "wall_time_seconds": 240,
                "memory_mb": 2048,
                "cpu_cores": 1.5,
                "disk_mb": 1536,
            },
        }
        try:
            # The trial deadline governs native work; bounded teardown may follow it.
            teardown_budget = (
                3 * controller.limits.cleanup_seconds
                + 10 * controller.limits.stop_seconds
                + 3 * controller.limits.service_seconds
            )
            result = await asyncio.wait_for(
                controller.run(run_spec), run_spec["limits"]["wall_time_seconds"] + teardown_budget
            )
            await save_operation_diagnostics()
            (local / "engineering-result.json").write_text(json.dumps(result, indent=2))
            async with AsyncSession(engine) as diagnostic_db:
                recorded = await diagnostic_db.scalar(
                    select(BenchmarkEvaluationAttempt)
                    .where(BenchmarkEvaluationAttempt.trial_id == trial.id)
                    .options(undefer(BenchmarkEvaluationAttempt.private_result))
                )
                if recorded is not None and recorded.private_result is not None:
                    native_result = store.private.open(
                        "evaluation-result:" + str(recorded.id), recorded.private_result
                    )
                    (local / "engineering-native-result.json").write_text(
                        json.dumps(native_result, indent=2)
                    )
            assert (
                result.get("execution") == "success" and result.get("evaluation") == "completed"
            ), result
            assert result["cleanup"] == "complete" and result["recording"] == "complete", result
            assert len(endpoint.requests) == 2 and not endpoint.errors
            with tarfile.open(fileobj=io.BytesIO(artifacts.values["native-artifacts"])) as archive:
                assert (
                    archive.extractfile("artifacts/workspace/result.txt").read()
                    == b"Driven through the Benchmark SDK\n"
                )
                assert (
                    archive.extractfile("artifacts/logs/artifacts/answer.txt").read()
                    == b"Native artifact\n"
                )
                if services_enabled:
                    assert (
                        archive.extractfile("artifacts/workspace/sidecar.txt").read()
                        == b"Native sidecar artifact\n"
                    )
            async with AsyncSession(engine) as db:
                evaluation = await db.scalar(
                    select(BenchmarkEvaluationAttempt)
                    .where(BenchmarkEvaluationAttempt.trial_id == trial.id)
                    .options(undefer(BenchmarkEvaluationAttempt.private_result))
                )
                assert evaluation.status == "completed" and evaluation.success is None
                native_result = store.private.open(
                    "evaluation-result:" + str(evaluation.id), evaluation.private_result
                )
                assert native_result["native"]["rewards"] == {"quality": 0.625}
                attempt = await db.get(
                    ExecutionAttempt, controller.host.agent_gateway.calls.session_id
                )
                assert attempt.connector_id == connector_id and attempt.status == "success"
                assert attempt.run_spec["limits"]["wall_time_seconds"] == 60
                events = list(
                    await db.scalars(
                        select(ExecutionEvent.payload).where(
                            ExecutionEvent.attempt_id == attempt.id
                        )
                    )
                )
                assert any(event["event"] == "tool.end" for event in events)
                assert credential not in json.dumps(events)
                source_id, source_digest = evaluation.id, evaluation.result_digest
                source_sequence = (await db.get(BenchmarkTrial, trial.id)).next_seq
            if mode == "separate" and connector_id == "pi":
                grading = EvaluationControls(store, evidence)
                grading_owner = await grading.create(
                    WORKSPACE,
                    trial.id,
                    source_id,
                    uuid.uuid4(),
                    deadline_at=datetime.now(UTC) + timedelta(seconds=180),
                )
                grading_evidence = EvidenceStore(grading, objects, bucket=bucket)
                replay_artifacts = Capture(grading_owner, grading_evidence)
                replay_coordinator = RootlessCoordinator(
                    runtime,
                    provider,
                    SimpleNamespace(
                        store=grading,
                        sink=lambda selected: replay_artifacts,
                        validate_capture=grading_evidence.validate_capture,
                        open_verified=grading_evidence.open_verified,
                    ),
                    limits=controller.limits,
                )
                replay = await replay_coordinator.evaluation(grading, grading_owner)
                hosts.append(replay.host)
                replay_result = await asyncio.wait_for(replay.run(), 180 + teardown_budget)
                (local / "engineering-regrade-result.json").write_text(
                    json.dumps(replay_result, indent=2)
                )
                assert (
                    replay_result["evaluation"] == "completed"
                    and replay_result["cleanup"] == "complete"
                ), replay_result
                assert "task" not in replay.host.environment.leases
                assert replay.host.agent_gateway is None
                async with AsyncSession(engine) as db:
                    replay_evaluation = await db.scalar(
                        select(BenchmarkEvaluationAttempt)
                        .where(
                            BenchmarkEvaluationAttempt.id
                            == uuid.UUID(replay_result["evaluation_id"])
                        )
                        .options(undefer(BenchmarkEvaluationAttempt.private_result))
                    )
                    replay_native = store.private.open(
                        "evaluation-result:" + str(replay_evaluation.id),
                        replay_evaluation.private_result,
                    )
                    (local / "engineering-regrade-native-result.json").write_text(
                        json.dumps(replay_native, indent=2)
                    )
                    assert replay_native["native"]["rewards"] == {"quality": 0.625}
                    original = await db.get(BenchmarkEvaluationAttempt, source_id)
                    assert original.result_digest == source_digest
                    assert (await db.get(BenchmarkTrial, trial.id)).next_seq == source_sequence
            async with AsyncSession(engine) as db:
                leases = list(
                    await db.scalars(
                        select(BenchmarkResourceLease).where(
                            BenchmarkResourceLease.trial_id == trial.id
                        )
                    )
                )
                assert leases and all(lease.state == "released" for lease in leases)
        finally:
            for host in reversed(hosts):
                scopes = [
                    (
                        host.scope,
                        str(
                            uuid.uuid5(
                                uuid.UUID(host.scope.session_id), "resource:" + host.WORKER_ID
                            )
                        ),
                    )
                ]
                if host.environment is not None:
                    scopes += [
                        (host.environment.scope, lease.handle)
                        for _, lease in host.environment.leases.values()
                        if lease.kind == "container"
                    ]
                for scope, handle in scopes:
                    with suppress(Exception):
                        native_container = await asyncio.to_thread(runtime.find, scope, handle)
                        if native_container is not None:
                            await asyncio.to_thread(
                                runtime.stop, native_container, grace_seconds=0, remove=True
                            )
                if host.environment is not None:
                    storage = RootlessVolumeRuntime(runtime)
                    for _, lease in reversed(list(host.environment.leases.values())):
                        if lease.kind not in {"volume", "network"}:
                            continue
                        with suppress(Exception):
                            lookup = (
                                storage.find if lease.kind == "volume" else runtime.find_network
                            )
                            remove = (
                                storage.remove if lease.kind == "volume" else runtime.remove_network
                            )
                            owned_native = await asyncio.to_thread(
                                lookup, host.environment.scope, lease.handle
                            )
                            if owned_native is not None:
                                await asyncio.to_thread(remove, owned_native)
                if host.channel is not None:
                    with suppress(Exception):
                        await host.channel.close(grace=0.2)
                await host.host_broker.close()
                await host.worker_broker.close()
            client.close()
