"""Real Pi/OpenCode participants in native tau2 with offline model responses."""

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
from app.benchmarks.driver import DriverLimits
from app.benchmarks.evaluation_controls import EvaluationControls
from app.benchmarks.evidence import EvidenceStore
from app.benchmarks.model_calls import ModelBinding
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
from app.runtime.model_gateway import ModelRoute
from app.runtime.registered_assets import RegisteredFile
from app.runtime.registered_files import load_registered_assets
from evalhive_runner.runner import RunnerPolicy
from minio import Minio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import undefer

from tests.fixtures.tau2_model_endpoint import Tau2ModelEndpoint
from tests.integration.test_benchmark_worker_host_native import WORKSPACE
from tests.runtime.authority_fixture import FixtureArtifacts, manifest

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_TAU2") != "1",
    reason="Requires pinned tau2 worker, native data and Harness runtime bundles",
)


@pytest_asyncio.fixture
async def native_engine(_engine, db_session):
    engine = create_async_engine(_engine.url, pool_size=5, max_overflow=5)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.parametrize("connector_id", ["pi", "opencode"])
async def test_native_user_tools_agent_steps_and_cold_regrade(native_engine, connector_id):
    from evalhive_tau2.installation import install
    from evalhive_tau2.worker import MANIFEST

    local = Path(os.environ["EVALHIVE_NATIVE_FIXTURE_DIR"]) / ("tau2-" + connector_id)
    local.mkdir(parents=True)
    host_root = os.environ["EVALHIVE_NATIVE_FIXTURE_HOST"] + "/" + local.name
    client = docker.DockerClient(base_url="unix:///run/user/11001/docker.sock", timeout=60)
    worker_image = client.images.get(
        os.getenv("EVALHIVE_NATIVE_TAU2_WORKER_IMAGE", "evalhive-benchmark-tau2:0.1.0")
    )
    task_image = client.images.get("evalhive-tau2-task:0.1.0")
    for image, repository in (
        (worker_image, "evalhive-benchmark-tau2"),
        (task_image, "evalhive-tau2-task"),
    ):
        assert image.tag(repository, "retained-" + image.id.removeprefix("sha256:")[:12])
    installed = local / "installed"
    receipt = install(
        os.environ["TAU2_DATA_DIR"],
        installed,
        domain="airline",
        revision="native-baseline-fc0055dc",
        worker_image=worker_image.id,
        task_image=task_image.id,
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
    declaration = {**deepcopy(MANIFEST), "image": worker_image.id}
    assets = load_registered_assets(
        PathMapping(installed_host, str(installed)),
        runtime,
        declaration,
        bindings={
            "harness.runtime": RegisteredFile(bundle.host_path, "sha256:" + bundle.fingerprint)
        },
    )
    engine = native_engine
    async with AsyncSession(engine) as db:
        task = Task(
            title="Native tau2 engineering acceptance", workspace_id=WORKSPACE, status="in_progress"
        )
        db.add(task)
        await db.flush()
        task_id = task.id
        await db.commit()
    store = TrialStore(engine, PrivateStore("native-tau2-fixture-key"))
    trial = await store.create(
        WORKSPACE,
        task_id=task_id,
        snapshot={"fixture": "native tau2"},
        deadline_at=datetime.now(UTC) + timedelta(seconds=360),
    )
    owner = await store.claim(WORKSPACE, trial.id, uuid.uuid4())
    objects = Minio(
        os.environ["EVALHIVE_EVIDENCE_MINIO"],
        access_key="fixture-evidence",
        secret_key="fixture-evidence-password",
        secure=False,
        http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=3, read=10), retries=False),
    )
    bucket = "tau2-engineering-" + uuid.uuid4().hex
    await asyncio.to_thread(objects.make_bucket, bucket)
    evidence = EvidenceStore(store, objects, bucket=bucket)

    class Capture(FixtureArtifacts):
        def __init__(self, selected_owner, selected_evidence):
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

    artifacts = Capture(owner, evidence)

    def facade(selected_store, selected_artifacts, selected_evidence):
        return SimpleNamespace(
            store=selected_store,
            sink=lambda selected: selected_artifacts,
            validate_capture=selected_evidence.validate_capture,
            open_verified=selected_evidence.open_verified,
        )

    provider = {**manifest(), "id": "evalhive.rootless"}
    coordinator = RootlessCoordinator(
        runtime,
        provider,
        facade(store, artifacts, evidence),
        limits=DriverLimits(
            phase_seconds=180,
            capture_seconds=90,
            cleanup_seconds=45,
            stop_seconds=10,
            service_seconds=15,
        ),
        runner_policy=RunnerPolicy(heartbeat_seconds=0.2, startup_seconds=45, command_seconds=30),
    )
    hosts = []
    agent_key, user_key = uuid.uuid4().hex, uuid.uuid4().hex
    try:
        with Tau2ModelEndpoint(agent_key, user_key) as endpoint:
            harness_spec = {
                "protocol_version": "1.0",
                "workspace_id": str(WORKSPACE),
                "task_id": str(task_id),
                "attempt_id": str(uuid.uuid4()),
                "task": {"prompt": "Bound by native prepared access"},
                "cwd": "/workspace",
                "config": {},
                "limits": {"max_turns": 4},
                "interaction": {"version": "1.0", "mode": "external_steps"},
                "model": {
                    "name": "fixture-agent",
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
                credentials={"model_api_key": agent_key},
                spool_root=local / "spool",
                model_gateway=True,
                auxiliary_models={
                    "user": ModelBinding(
                        "aux-user",
                        ModelRoute("fixture-user", endpoint.url, user_key, uuid.uuid4().hex),
                        ("driving",),
                    )
                },
            )
            hosts.append(controller.host)
            run_spec = {
                "protocol_version": "1.0",
                "scope": {
                    "workspace_id": str(WORKSPACE),
                    "trial_id": str(trial.id),
                    "generation": owner.generation,
                },
                "case": receipt["cases"][0],
                "config": {},
                "credential_refs": {"aux-user": "fixture-user-reference"},
                "models": {
                    "user": {
                        "name": "fixture-user",
                        "endpoint": endpoint.url,
                        "credential_ref": "aux-user",
                    }
                },
                "limits": {
                    "wall_time_seconds": 360,
                    "memory_mb": 2048,
                    "cpu_cores": 1.5,
                    "disk_mb": 2048,
                    "model_requests": 8,
                },
            }
            result = await asyncio.wait_for(controller.run(run_spec), 600)
            (local / "engineering-result.json").write_text(json.dumps(result, indent=2))
            async with AsyncSession(engine) as db:
                journal = list(
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
                diagnostics = {
                    "journal": [{"event": row.event, "public": row.public_data} for row in journal],
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
                }
                (local / "engineering-diagnostics.json").write_text(
                    json.dumps(diagnostics, indent=2)
                    .replace(agent_key, "[redacted]")
                    .replace(user_key, "[redacted]")
                )
            assert (
                result.get("execution") == "success" and result.get("evaluation") == "completed"
            ), result
            assert result["recording"] == "complete" and result["cleanup"] == "complete", result
            assert (
                len(endpoint.requests["agent"]) == len(endpoint.requests["user"]) == 2
                and not endpoint.errors
            )
            trace_data = artifacts.values["trace"]
            with tarfile.open(fileobj=io.BytesIO(trace_data)) as archive:
                simulation = json.loads(archive.extractfile("trace/record.json").read())[
                    "simulation"
                ]
            assert simulation["termination_reason"] == "user_stop"
            tool_messages = [
                message for message in simulation["messages"] if message["role"] == "tool"
            ]
            assert {message["id"] for message in tool_messages} == {"arithmetic-a", "arithmetic-b"}
            assert {message["content"] for message in tool_messages} == {"4.0", "6.0"}
            from evalhive_tau2.native import catalog
            from tau2.data_model.simulation import SimulationRun
            from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation

            reference = await asyncio.to_thread(
                evaluate_simulation,
                SimulationRun.model_validate(simulation),
                catalog(os.environ["TAU2_DATA_DIR"], "airline")[0],
                EvaluationType.ALL,
                False,
                "airline",
                strict_replay=True,
            )
            async with AsyncSession(engine) as db:
                source = await db.scalar(
                    select(BenchmarkEvaluationAttempt)
                    .where(BenchmarkEvaluationAttempt.trial_id == trial.id)
                    .options(undefer(BenchmarkEvaluationAttempt.private_result))
                )
                native = store.private.open(
                    "evaluation-result:" + str(source.id), source.private_result
                )
                assert native["native"]["termination"] == "user_stop" and native["native"][
                    "reward"
                ] in (0.0, 1.0)
                assert native["native"]["reward"] == reference.reward
                source_id, source_digest = source.id, source.result_digest
                source_sequence = (await db.get(BenchmarkTrial, trial.id)).next_seq
                attempt = await db.get(
                    ExecutionAttempt, controller.host.agent_gateway.calls.session_id
                )
                assert attempt.status == "success" and attempt.connector_id == connector_id
                assert attempt.run_spec["interaction"]["mode"] == "external_steps"
                events = list(
                    await db.scalars(
                        select(ExecutionEvent.payload).where(
                            ExecutionEvent.attempt_id == attempt.id
                        )
                    )
                )
                assert any(event["event"] == "tool.request" for event in events)
                assert agent_key not in json.dumps(events) and user_key not in json.dumps(events)
            usage = await trial_usage(store, WORKSPACE, trial.id)
            assert usage["valid"], usage["errors"]
            (local / "engineering-usage.json").write_text(json.dumps(usage, indent=2))
            user_usage = [group for group in usage["groups"] if group["role"] == "user"]
            assert len(user_usage) == 1 and user_usage[0]["calls"] == 2
            assert user_usage[0]["tokens"]["input_tokens"]["value"] == 14
            assert any(group["role"] == "agent" for group in usage["groups"])
            if connector_id == "pi":
                grading = EvaluationControls(store, evidence)
                grade_owner = await grading.create(
                    WORKSPACE,
                    trial.id,
                    source_id,
                    uuid.uuid4(),
                    deadline_at=datetime.now(UTC) + timedelta(seconds=240),
                )
                grade_evidence = EvidenceStore(grading, objects, bucket=bucket)
                cold_artifacts = Capture(grade_owner, grade_evidence)
                cold_coordinator = RootlessCoordinator(
                    runtime,
                    provider,
                    facade(grading, cold_artifacts, grade_evidence),
                    limits=coordinator.limits,
                )
                cold = await cold_coordinator.evaluation(grading, grade_owner)
                hosts.append(cold.host)
                cold_result = await asyncio.wait_for(cold.run(), 420)
                (local / "engineering-regrade-result.json").write_text(
                    json.dumps(cold_result, indent=2)
                )
                assert (
                    cold_result["evaluation"] == "completed"
                    and cold_result["recording"] == "complete"
                    and cold_result["cleanup"] == "complete"
                ), cold_result
                assert (
                    cold.host.agent_gateway is None and "task" not in cold.host.environment.leases
                )
                assert len(endpoint.requests["agent"]) == len(endpoint.requests["user"]) == 2
                async with AsyncSession(engine) as db:
                    cold_row = await db.scalar(
                        select(BenchmarkEvaluationAttempt)
                        .where(
                            BenchmarkEvaluationAttempt.id == uuid.UUID(cold_result["evaluation_id"])
                        )
                        .options(undefer(BenchmarkEvaluationAttempt.private_result))
                    )
                    actual = store.private.open(
                        "evaluation-result:" + str(cold_row.id), cold_row.private_result
                    )
                    assert actual["metrics"] == native["metrics"]
                    assert (
                        await db.get(BenchmarkEvaluationAttempt, source_id)
                    ).result_digest == source_digest
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
                    selected = await asyncio.to_thread(runtime.find, scope, handle)
                    if selected is not None:
                        await asyncio.to_thread(
                            runtime.stop, selected, grace_seconds=0, remove=True
                        )
            if host.channel is not None:
                with suppress(Exception):
                    await host.channel.close(grace=0.2)
            await host.host_broker.close()
            await host.worker_broker.close()
        client.close()
