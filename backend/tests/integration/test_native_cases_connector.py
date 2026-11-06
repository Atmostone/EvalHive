"""The installed native case package uses the same coordinator for both harnesses."""

import asyncio
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

import docker
import pytest
import pytest_asyncio
from app.benchmarks.agent_calls import AgentCallStore
from app.benchmarks.agent_gateway import AgentGateway
from app.benchmarks.driver import DriverLimits, TrialDriver
from app.benchmarks.harness_execution import HarnessExecution
from app.benchmarks.harness_transport import RootlessHarnessTransport
from app.benchmarks.private_store import PrivateStore
from app.benchmarks.recovery import RecoveryService
from app.benchmarks.trials import TrialStore
from app.benchmarks.worker_host import RootlessWorkerHost
from app.harnesses.catalog import get_manifest
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_trial import BenchmarkResourceLease
from app.models.execution_attempt import ExecutionAttempt, ExecutionEvent
from app.models.task import Task
from app.runtime.bundles import install_bundle
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.lease_codec import decode_lease
from app.runtime.managed_docker import RootlessDockerRuntime
from app.runtime.managed_exec import OwnedExecRuntime
from app.runtime.managed_spec import PathMapping, Quota, RuntimePolicy
from app.runtime.reconcile import RootlessReconciler
from app.runtime.registered_assets import RegisteredFile, RootlessAssets
from evalhive_runner.runner import RunnerPolicy
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import undefer

from tests.integration.test_benchmark_worker_host_native import WORKSPACE
from tests.runtime.agent_worker_fixture import SERVER
from tests.runtime.authority_fixture import FixtureArtifacts, manifest

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_CASES") != "1", reason="Requires explicitly built native case image"
)


@pytest_asyncio.fixture
async def native_engine(_engine, db_session):
    pooled = create_async_engine(_engine.url, pool_size=5, max_overflow=5)
    try:
        yield pooled
    finally:
        await pooled.dispose()


@pytest.mark.parametrize("connector_id", ["pi", "opencode"])
async def test_installed_case_worker_runs_and_grades_through_real_harness(
    native_engine, connector_id
):
    # This import belongs to the engineering test process only, never backend code.
    from evalhive_cases.dataset import build_receipt
    from evalhive_cases.worker import MANIFEST

    engine = native_engine
    local = Path(os.environ["EVALHIVE_NATIVE_FIXTURE_DIR"]) / ("cases-" + connector_id)
    host_root = os.environ["EVALHIVE_NATIVE_FIXTURE_HOST"] + "/cases-" + connector_id
    (local / "bundle").mkdir(parents=True)
    source = local / "dataset"
    (source / "fixture").mkdir(parents=True)
    document = {
        "id": "native-engineering",
        "suite": "fixture",
        "category": "engineering",
        "input": {
            "title": "Native SDK task",
            "description": "Run the requested native shell check and finish.",
        },
        "gold": {"reference_answer": "Completed", "rubric": {"private": "never-send-to-agent"}},
    }
    (source / "fixture/case.json").write_text(json.dumps(document))
    client = docker.DockerClient(base_url="unix:///run/user/11001/docker.sock", timeout=15)
    runtime = RootlessDockerRuntime(
        client,
        RuntimePolicy(
            PathMapping(host_root, str(local)),
            (
                PathMapping(host_root + "/bundle", str(local / "bundle")),
                PathMapping(host_root + "/dataset", str(source)),
            ),
        ),
    )
    worker_image = await asyncio.to_thread(runtime.resolve_image, "evalhive-benchmark-cases:0.1.0")
    task_image = await asyncio.to_thread(
        runtime.resolve_image, "evalhive-environment-workspace:0.1.0"
    )
    receipt = build_receipt(source, "fixture", "engineering-1", worker_image["image_id"])
    (source / "installation.json").write_text(json.dumps(receipt))
    harness = get_manifest(connector_id)
    installation = deepcopy(harness)
    installation["requirements"]["runtime_bundle"]["image"] = (
        "evalhive-" + connector_id + "-bundle:development"
    )
    bundle = await asyncio.to_thread(
        install_bundle,
        runtime,
        installation,
        PathMapping(host_root + "/bundle", str(local / "bundle")),
    )
    declaration = deepcopy(MANIFEST)
    declaration["image"] = worker_image["image_id"]
    provider_id = "evalhive.rootless"
    provider_manifest = {**manifest(), "id": provider_id}
    registration = {
        "format_version": "1.0",
        "connector_id": declaration["id"],
        "connector_version": declaration["version"],
        "provider_id": provider_id,
        "worker_asset": "cases.worker",
        "assets": {
            "cases.worker": {
                "kind": "container",
                "role": "worker",
                "image": worker_image["image_id"],
                "limits": asdict(Quota(256, 0.25, 128)),
                "minimum": {"memory_mb": 256, "cpu_cores": 0.25, "disk_mb": 16},
                "mounts": [{"file_id": "dataset", "target": "/dataset", "read_only": True}],
            },
            "evalhive-cases.task": {
                "kind": "container",
                "role": "environment",
                "image": task_image["image_id"],
                "limits": asdict(Quota(1024, 1, 256)),
                "minimum": {"memory_mb": 256, "cpu_cores": 1, "disk_mb": 16},
                "command": [
                    "python",
                    "-c",
                    "from pathlib import Path;import time;Path('/workspace/result.txt').write_text('Prepared through the Benchmark SDK\\n');exec("
                    + repr(SERVER)
                    + ");time.sleep(300)",
                ],
                "mounts": [
                    {"file_id": "supervisor", "target": "/evalhive-runtime", "read_only": True}
                ],
                "outputs": {
                    "workspace": {
                        "path": "/workspace",
                        "visibility": "user",
                        "required": True,
                        "max_archive_bytes": 2 * 1024**2,
                    }
                },
            },
        },
    }
    assets = RootlessAssets(
        registration,
        runtime,
        files={
            "dataset": RegisteredFile(host_root + "/dataset", receipt["source_digest"]),
            "supervisor": RegisteredFile(bundle.host_path, "sha256:" + bundle.fingerprint),
        },
        connector_id=declaration["id"],
        connector_version=declaration["version"],
    )
    async with AsyncSession(engine) as db:
        task = Task(
            title="Native case package acceptance", workspace_id=WORKSPACE, status="in_progress"
        )
        db.add(task)
        await db.flush()
        task_id = task.id
        await db.commit()
    store = TrialStore(engine, PrivateStore("native-case-package-fixture-key"))
    trial = await store.create(
        WORKSPACE,
        task_id=task_id,
        snapshot={"fixture": "native case package"},
        deadline_at=datetime.now(UTC) + timedelta(seconds=180),
    )
    owner = await store.claim(WORKSPACE, trial.id, uuid.uuid4())

    class Capture(FixtureArtifacts):
        async def save(self, scope, grant, chunks, *, publication=None):
            async with AsyncSession(engine) as db:
                rows = list(
                    await db.scalars(
                        select(BenchmarkResourceLease)
                        .where(
                            BenchmarkResourceLease.trial_id == trial.id,
                            BenchmarkResourceLease.kind == "exec",
                        )
                        .options(undefer(BenchmarkResourceLease.private_value))
                    )
                )
                assert rows and all(row.state == "stopped" for row in rows)
                leases = [
                    decode_lease(store.private.open("lease:" + str(row.id), row.private_value))
                    for row in rows
                ]
            for lease in leases:
                status = await asyncio.to_thread(
                    OwnedExecRuntime(runtime).control, lease.native, "status"
                )
                assert status["main_done"] and status["stopped"] and status["children"] == 0
            return await super().save(scope, grant, chunks, publication=publication)

    artifacts = Capture()
    factories = {
        provider_id: lambda authority: RootlessDockerProvider(
            runtime, authority, provider_manifest, artifacts=artifacts
        )
    }
    credential = uuid.uuid4().hex

    def agent_factory(host):
        calls = AgentCallStore(store, owner, host.worker_actor)
        run_spec = {
            "protocol_version": "1.0",
            "workspace_id": str(WORKSPACE),
            "task_id": str(task_id),
            "attempt_id": str(calls.session_id),
            "task": {"prompt": "Frozen by prepared access"},
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
                "runtime_bundle_image_id": bundle.image_id,
                "runtime_bundle_fingerprint": bundle.fingerprint,
            },
        }

        async def transport():
            return await RootlessHarnessTransport(
                host, host.plan, host.prepared, bundle, harness, credential_ref="agent.credentials"
            )()

        return AgentGateway(
            calls,
            HarnessExecution(
                calls,
                harness,
                run_spec,
                transport,
                local / "spool" / str(calls.session_id) / "events.sqlite",
                private_values=[credential],
                policy=RunnerPolicy(heartbeat_seconds=0.2, startup_seconds=20, command_seconds=20),
            ),
        )

    host = RootlessWorkerHost(
        store,
        owner,
        declaration,
        assets,
        factories,
        agent_factory=agent_factory,
        host_private_inputs={
            "agent.credentials": json.dumps({"model_api_key": credential}).encode()
        },
    )
    recovery = RecoveryService(
        store,
        host.host_actor,
        {provider_id: lambda authority: RootlessReconciler(runtime, authority, provider_id)},
    )
    controller = TrialDriver(
        host,
        recovery,
        capture_providers=factories,
        capture_validator=artifacts.validate_capture,
        limits=DriverLimits(
            phase_seconds=90, capture_seconds=10, cleanup_seconds=25, service_seconds=10
        ),
    )
    run_spec = {
        "protocol_version": "1.0",
        "scope": {
            "workspace_id": str(WORKSPACE),
            "trial_id": str(trial.id),
            "generation": owner.generation,
        },
        "case": receipt["cases"][0],
        "config": {
            "evaluation_mode": "reference_exact",
            "resources": [{"id": "tools", "kind": "tools", "config": {"names": ["bash"]}}],
        },
        "credential_refs": {},
        "models": {},
        "limits": {"wall_time_seconds": 180, "memory_mb": 1536, "cpu_cores": 1.25, "disk_mb": 64},
    }
    try:
        result = await asyncio.wait_for(controller.run(run_spec), 150)
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
            assert evaluation.status == "completed" and evaluation.success is True
            attempt = await db.get(ExecutionAttempt, host.agent_gateway.calls.session_id)
            assert attempt.connector_id == connector_id and attempt.status == "success"
            assert "never-send-to-agent" not in json.dumps(attempt.run_spec)
            events = list(
                await db.scalars(
                    select(ExecutionEvent.payload).where(ExecutionEvent.attempt_id == attempt.id)
                )
            )
            assert any(event["event"] == "tool.end" for event in events)
            assert credential not in json.dumps(events)
            leases = list(
                await db.scalars(
                    select(BenchmarkResourceLease).where(
                        BenchmarkResourceLease.trial_id == trial.id
                    )
                )
            )
            assert leases and all(lease.state == "released" for lease in leases)
    finally:
        scopes = [
            (
                host.scope,
                str(uuid.uuid5(uuid.UUID(host.scope.session_id), "resource:" + host.WORKER_ID)),
            )
        ]
        if host.environment is not None:
            scopes.extend(
                (host.environment.scope, lease.handle)
                for _, lease in host.environment.leases.values()
                if lease.kind == "container"
            )
        for scope, handle in scopes:
            with suppress(Exception):
                native = await asyncio.to_thread(runtime.find, scope, handle)
                if native is not None:
                    await asyncio.to_thread(runtime.stop, native, grace_seconds=0, remove=True)
        if host.channel is not None:
            with suppress(Exception):
                await host.channel.close(grace=0.2)
        await host.host_broker.close()
        await host.worker_broker.close()
        client.close()
