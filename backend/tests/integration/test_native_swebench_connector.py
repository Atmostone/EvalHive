"""An original offline classic task uses isolated native grading with both harnesses."""

import asyncio
import io
import json
import os
import shutil
import tarfile
import traceback
import uuid
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict
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
from app.benchmarks.usage import trial_usage
from app.harnesses.catalog import get_manifest
from app.models.benchmark_command import BenchmarkCommand
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_trial import BenchmarkJournalEntry, BenchmarkResourceLease, BenchmarkTrial
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
    os.getenv("EVALHIVE_NATIVE_SWEBENCH") != "1",
    reason="Requires pinned native SWEbench images, official data and runtime bundles",
)


@pytest_asyncio.fixture
async def native_engine(_engine, db_session):
    engine = create_async_engine(_engine.url, pool_size=5, max_overflow=5)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.parametrize("connector_id", ["pi", "opencode"])
async def test_original_classic_patch_and_native_verdict(native_engine, connector_id):
    from evalhive_swebench.worker import MANIFEST

    local = Path(os.environ["EVALHIVE_NATIVE_FIXTURE_DIR"]) / ("swebench-" + connector_id)
    local.mkdir(parents=True)
    host_root = os.environ["EVALHIVE_NATIVE_FIXTURE_HOST"] + "/" + local.name
    client = docker.DockerClient(base_url="unix:///run/user/11001/docker.sock", timeout=60)
    image = client.images.get("evalhive-benchmark-swebench:0.1.0")
    assert image.tag("evalhive-benchmark-swebench", "retained-" + image.id[7:19])
    installed = local / "installed"
    source = Path(os.environ["EVALHIVE_SWEBENCH_INSTALLATION"])
    shutil.copytree(source, installed)
    receipt = json.loads((installed / "worker-data/installation.json").read_text())
    assert receipt["worker_image"] == image.id and receipt["provenance"]["subset"] == "verified"
    selected = next(
        case for case in receipt["cases"] if case["ref"]["case_id"] == "pytest-dev__pytest-10051"
    )
    assert len(receipt["cases"]) == 500
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
    declaration = {**deepcopy(MANIFEST), "image": image.id}
    assets = load_registered_assets(
        PathMapping(installed_host, str(installed)),
        runtime,
        declaration,
        bindings={
            "harness.runtime": RegisteredFile(bundle.host_path, "sha256:" + bundle.fingerprint)
        },
    )
    async with AsyncSession(native_engine) as db:
        task = Task(
            title="Native SWEbench engineering acceptance",
            workspace_id=WORKSPACE,
            status="in_progress",
        )
        db.add(task)
        await db.flush()
        task_id = task.id
        await db.commit()
    store = TrialStore(native_engine, PrivateStore("native-swebench-fixture-key"))
    trial = await store.create(
        WORKSPACE,
        task_id=task_id,
        snapshot={"fixture": "native SWEbench"},
        deadline_at=datetime.now(UTC) + timedelta(seconds=600),
    )
    owner = await store.claim(WORKSPACE, trial.id, uuid.uuid4())
    objects = Minio(
        os.environ["EVALHIVE_EVIDENCE_MINIO"],
        access_key="fixture-evidence",
        secret_key="fixture-evidence-password",
        secure=False,
        http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=3, read=10), retries=False),
    )
    bucket = "swebench-engineering-" + uuid.uuid4().hex
    await asyncio.to_thread(objects.make_bucket, bucket)
    evidence = EvidenceStore(store, objects, bucket=bucket)

    class Capture(FixtureArtifacts):
        def __init__(self, selected_owner=owner, selected_evidence=evidence):
            super().__init__()
            self.owner, self.evidence = selected_owner, selected_evidence

        async def save(self, scope, grant, chunks, *, publication=None):
            data = bytearray()

            async def observed():
                async for chunk in chunks:
                    data.extend(chunk)
                    yield chunk

            result = await self.evidence.sink(self.owner).save(
                scope, grant, observed(), publication=publication
            )
            self.values[grant.artifact_id] = bytes(data)
            return result

    artifacts = Capture()
    facade = SimpleNamespace(
        store=store,
        sink=lambda selected: artifacts,
        validate_capture=evidence.validate_capture,
        open_verified=evidence.open_verified,
    )
    coordinator = RootlessCoordinator(
        runtime,
        {**manifest(), "id": "evalhive.rootless"},
        facade,
        resource_finalizers=False,
        limits=DriverLimits(
            phase_seconds=600,
            capture_seconds=180,
            cleanup_seconds=150,
            stop_seconds=10,
            service_seconds=15,
        ),
        runner_policy=RunnerPolicy(heartbeat_seconds=0.2, startup_seconds=45, command_seconds=30),
    )
    host = None
    hosts = []
    key = uuid.uuid4().hex
    try:
        tool_script = (
            "import subprocess\nfrom pathlib import Path\n"
            "assert not any(Path(p).exists() for p in ('/dataset','/stage','/records','/checkpoint','/evalhive-results'))\n"
            "p=Path('/testbed/src/_pytest/__init__.py')\n"
            "assert p.is_file()\n"
            "p.write_text(p.read_text()+'\\n# EvalHive engineering smoke.\\n')\n"
            "subprocess.run(['git','diff','--stat'],check=True)\n"
        )
        with NativeModelEndpoint(key, tool_script=tool_script) as endpoint:
            harness_spec = {
                "protocol_version": "1.0",
                "workspace_id": str(WORKSPACE),
                "task_id": str(task_id),
                "attempt_id": str(uuid.uuid4()),
                "task": {"prompt": "Bound by native prepared access"},
                "cwd": "/workspace",
                "config": {"apiTimeoutSeconds": 120} if connector_id == "opencode" else {},
                "limits": {"max_turns": 8},
                "interaction": {"version": "1.0", "mode": "single_turn"},
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
                credentials={"model_api_key": key},
                spool_root=local / "spool",
                model_gateway=True,
            )
            host = controller.host
            hosts.append(host)
            measure = host._measure

            async def observed_capacity():
                capacity = await measure()
                (local / "engineering-capacity.json").write_text(
                    json.dumps(asdict(capacity), indent=2)
                )
                return capacity

            host._measure = observed_capacity
            install_plan = host.install_plan

            async def observed_plan(*args, **kwargs):
                try:
                    return await install_plan(*args, **kwargs)
                except Exception:
                    (local / "engineering-plan-failure.txt").write_text(
                        traceback.format_exc().replace(key, "[redacted]")
                    )
                    raise

            host.install_plan = observed_plan
            spec = {
                "protocol_version": "1.0",
                "scope": {
                    "workspace_id": str(WORKSPACE),
                    "trial_id": str(trial.id),
                    "generation": owner.generation,
                },
                "case": selected,
                "config": {},
                "credential_refs": {},
                "models": {},
                "limits": {
                    "wall_time_seconds": 600,
                    "memory_mb": 4096,
                    "cpu_cores": 2.5,
                    "disk_mb": 2048,
                    "model_requests": 8,
                },
            }
            samples = []

            async def observe_memory():
                while True:
                    if host.environment is not None:
                        for name, (_, lease) in list(host.environment.leases.items()):
                            if lease.kind != "container":
                                continue
                            try:
                                container = await asyncio.to_thread(
                                    runtime.find, host.environment.scope, lease.handle
                                )
                                if container is None:
                                    continue
                                native_container = client.containers.get(container.container_id)
                                if native_container.status != "running":
                                    continue
                                observed = await asyncio.to_thread(
                                    native_container.exec_run,
                                    [
                                        "/bin/cat",
                                        "/sys/fs/cgroup/memory.peak",
                                        "/sys/fs/cgroup/memory.swap.current",
                                        "/sys/fs/cgroup/memory.events",
                                    ],
                                )
                                samples.append(
                                    {
                                        "resource": name,
                                        "observed_at": datetime.now(UTC).isoformat(),
                                        "exit_code": observed.exit_code,
                                        "memory": observed.output.decode(errors="replace"),
                                    }
                                )
                            except Exception as error:
                                samples.append(
                                    {"resource": name, "exception": type(error).__name__}
                                )
                        (local / "engineering-memory.json").write_text(
                            json.dumps(samples[-200:], indent=2)
                        )
                    await asyncio.sleep(8)

            observing = asyncio.create_task(observe_memory())
            try:
                result = await asyncio.wait_for(controller.run(spec), 900)
            finally:
                observing.cancel()
                await asyncio.gather(observing, return_exceptions=True)
            observations = {}
            if host.environment is not None:
                for name, (_, lease) in host.environment.leases.items():
                    if lease.kind != "container":
                        continue
                    try:
                        container = await asyncio.to_thread(
                            runtime.find, host.environment.scope, lease.handle
                        )
                        if container is None:
                            observations[name] = {"missing": True}
                            continue
                        native_container = client.containers.get(container.container_id)
                        observations[name] = {"state": native_container.attrs["State"]}
                        log = await asyncio.to_thread(native_container.logs, tail=60)
                        (local / ("engineering-container-" + name + ".log")).write_text(
                            log.decode(errors="replace").replace(key, "[redacted]")
                        )
                    except Exception as error:
                        observations[name] = {"exception": type(error).__name__}
            (local / "engineering-container-states.json").write_text(
                json.dumps(observations, indent=2)
            )
            (local / "engineering-result.json").write_text(json.dumps(result, indent=2))
            (local / "engineering-model-requests.json").write_text(
                json.dumps(endpoint.requests, indent=2).replace(key, "[redacted]")
            )
            async with AsyncSession(native_engine) as db:
                events = list(
                    await db.scalars(
                        select(ExecutionEvent.payload).where(
                            ExecutionEvent.attempt_id == host.agent_gateway.calls.session_id
                        )
                    )
                )
                (local / "engineering-events.json").write_text(
                    json.dumps(events, indent=2).replace(key, "[redacted]")
                )
                rows = list(
                    await db.scalars(
                        select(BenchmarkJournalEntry)
                        .where(BenchmarkJournalEntry.trial_id == trial.id)
                        .order_by(BenchmarkJournalEntry.seq)
                    )
                )
                commands = list(
                    await db.scalars(
                        select(BenchmarkCommand)
                        .where(BenchmarkCommand.trial_id == trial.id)
                        .options(undefer(BenchmarkCommand.private_response))
                    )
                )
                (local / "engineering-diagnostics.json").write_text(
                    json.dumps(
                        {
                            "journal": [
                                {"event": row.event, "public": row.public_data} for row in rows
                            ],
                            "commands": [
                                {
                                    "method": row.method,
                                    "state": row.state,
                                    "response": store.private.open(
                                        "command-response:" + str(row.id), row.private_response
                                    )
                                    if row.private_response
                                    else None,
                                }
                                for row in commands
                            ],
                            "model_errors": endpoint.errors,
                        },
                        indent=2,
                    ).replace(key, "[redacted]")
                )
                attempt = await db.get(ExecutionAttempt, host.agent_gateway.calls.session_id)
                if attempt is not None:
                    (local / "engineering-attempt.json").write_text(
                        json.dumps(
                            {"status": attempt.status, "result": attempt.result}, indent=2
                        ).replace(key, "[redacted]")
                    )
            folder = local / "engineering-evidence"
            folder.mkdir()
            for name, data in artifacts.values.items():
                (folder / (name + ".tar")).write_bytes(data)
            assert (
                result.get("execution") == "success" and result.get("evaluation") == "completed"
            ), result
            assert result["recording"] == result["cleanup"] == "complete", result
            assert len(endpoint.requests) == 2 and not endpoint.errors, endpoint.errors
            tools = [event for event in events if event["event"] == "tool.end"]
            assert len(tools) == 1 and all(
                event["data"]["status"] == "completed" for event in tools
            )
            assert key not in json.dumps(events)
            async with AsyncSession(native_engine) as db:
                attempt = await db.get(ExecutionAttempt, host.agent_gateway.calls.session_id)
                assert attempt.status == "success" and attempt.connector_id == connector_id
                evaluation = await db.scalar(
                    select(BenchmarkEvaluationAttempt)
                    .where(BenchmarkEvaluationAttempt.trial_id == trial.id)
                    .options(undefer(BenchmarkEvaluationAttempt.private_result))
                )
                graded = store.private.open(
                    "evaluation-result:" + str(evaluation.id), evaluation.private_result
                )
                assert graded["success"] is False and graded["completeness"]["native_verdict"]
                source_id, source_digest = evaluation.id, evaluation.result_digest
                source_sequence = (await db.get(BenchmarkTrial, trial.id)).next_seq
                leases = list(
                    await db.scalars(
                        select(BenchmarkResourceLease)
                        .where(BenchmarkResourceLease.trial_id == trial.id)
                        .options(undefer(BenchmarkResourceLease.private_value))
                    )
                )
                assert leases and all(lease.state == "released" for lease in leases)
            with tarfile.open(fileobj=io.BytesIO(artifacts.values["patch"])) as archive:
                patch = archive.extractfile("checkpoint/patch.diff").read()
            assert b"EvalHive engineering smoke" in patch
            assert len(patch) < 8192
            assert not graded["native"]["metadata"]["cached_report_used"]
            assert graded["native"]["metadata"]["instance_id"] == selected["ref"]["case_id"]
            usage = await trial_usage(store, WORKSPACE, trial.id)
            assert usage["valid"] and sum(group["calls"] for group in usage["groups"]) == 2
            if connector_id == "pi":
                grading = EvaluationControls(store, evidence)
                grading_owner = await grading.create(
                    WORKSPACE,
                    trial.id,
                    source_id,
                    uuid.uuid4(),
                    deadline_at=datetime.now(UTC) + timedelta(seconds=600),
                )
                replay_evidence = EvidenceStore(grading, objects, bucket=bucket)
                replay_artifacts = Capture(grading_owner, replay_evidence)
                replay_coordinator = RootlessCoordinator(
                    runtime,
                    {**manifest(), "id": "evalhive.rootless"},
                    SimpleNamespace(
                        store=grading,
                        sink=lambda selected: replay_artifacts,
                        validate_capture=replay_evidence.validate_capture,
                        open_verified=replay_evidence.open_verified,
                    ),
                    limits=controller.limits,
                )
                replay = await replay_coordinator.evaluation(grading, grading_owner)
                hosts.append(replay.host)
                replay_result = await asyncio.wait_for(replay.run(), 900)
                (local / "engineering-regrade-result.json").write_text(
                    json.dumps(replay_result, indent=2)
                )
                assert (
                    replay_result["evaluation"] == "completed"
                    and replay_result["cleanup"] == "complete"
                ), replay_result
                assert (
                    replay.host.agent_gateway is None
                    and "task" not in replay.host.environment.leases
                )
                assert len(endpoint.requests) == 2
                async with AsyncSession(native_engine) as db:
                    reread = await db.get(
                        BenchmarkEvaluationAttempt,
                        uuid.UUID(replay_result["evaluation_id"]),
                        options=[undefer(BenchmarkEvaluationAttempt.private_result)],
                    )
                    replay_native = store.private.open(
                        "evaluation-result:" + str(reread.id), reread.private_result
                    )
                    (local / "engineering-regrade-native.json").write_text(
                        json.dumps(replay_native, indent=2)
                    )
                    assert (
                        replay_native["success"] is False
                        and replay_native["completeness"]["native_verdict"]
                    )
                    assert (
                        replay_native["native"]["metadata"]["run_id"]
                        != graded["native"]["metadata"]["run_id"]
                    )
                    assert not replay_native["native"]["metadata"]["cached_report_used"]
                    assert (
                        await db.get(BenchmarkEvaluationAttempt, source_id)
                    ).result_digest == source_digest
                    assert (await db.get(BenchmarkTrial, trial.id)).next_seq == source_sequence
    finally:
        for host in reversed(hosts):
            scopes = [
                (
                    host.scope,
                    str(uuid.uuid5(uuid.UUID(host.scope.session_id), "resource:" + host.WORKER_ID)),
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
                    container = await asyncio.to_thread(runtime.find, scope, handle)
                    if container is not None:
                        await asyncio.to_thread(
                            runtime.stop, container, grace_seconds=0, remove=True
                        )
            if host.channel is not None:
                with suppress(Exception):
                    await host.channel.close(grace=0.2)
            if host.environment is not None:
                storage = RootlessVolumeRuntime(runtime)
                for _, lease in reversed(list(host.environment.leases.values())):
                    if lease.kind not in {"volume", "network"}:
                        continue
                    with suppress(Exception):
                        lookup = storage.find if lease.kind == "volume" else runtime.find_network
                        remove = (
                            storage.remove if lease.kind == "volume" else runtime.remove_network
                        )
                        owned = await asyncio.to_thread(
                            lookup, host.environment.scope, lease.handle
                        )
                        if owned is not None:
                            await asyncio.to_thread(remove, owned)
            await host.host_broker.close()
            await host.worker_broker.close()
        client.close()
