"""The original Filesystem task runs through both real Harness SDK connectors."""

import asyncio
import io
import json
import os
import tarfile
import traceback
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
from app.benchmarks.evidence import EvidenceStore
from app.benchmarks.private_store import PrivateStore
from app.benchmarks.trials import TrialStore
from app.benchmarks.usage import trial_usage
from app.harnesses.catalog import get_manifest
from app.models.benchmark_command import BenchmarkCommand
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_trial import BenchmarkJournalEntry, BenchmarkResourceLease
from app.models.execution_attempt import ExecutionAttempt, ExecutionEvent
from app.models.task import Task
from app.runtime.bundles import install_bundle
from app.runtime.managed_docker import RootlessDockerRuntime
from app.runtime.managed_spec import PathMapping, RuntimePolicy
from app.runtime.registered_assets import RegisteredFile
from app.runtime.registered_files import load_registered_assets
from evalhive_runner.runner import RunnerPolicy
from minio import Minio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import undefer

from tests.fixtures.mcpmark_browser_endpoint import MCPMarkBrowserEndpoint
from tests.fixtures.mcpmark_model_endpoint import MCPMarkModelEndpoint
from tests.fixtures.mcpmark_postgres_endpoint import MCPMarkPostgresEndpoint
from tests.integration.test_benchmark_worker_host_native import WORKSPACE
from tests.runtime.authority_fixture import FixtureArtifacts, manifest

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_MCPMARK") != "1",
    reason="Requires the pinned MCPMark wheel, original input and built runtime images",
)


@pytest_asyncio.fixture
async def native_engine(_engine, db_session):
    engine = create_async_engine(_engine.url, pool_size=5, max_overflow=5)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.parametrize("connector_id", ["pi", "opencode"])
async def test_original_filesystem_native_verdict_and_files_through_real_harness(
    native_engine, connector_id
):
    await run_native_mcpmark_case(native_engine, connector_id, service="filesystem")


@pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_MCPMARK_BROWSER") != "1",
    reason="Requires the pinned browser images and available native public website",
)
@pytest.mark.parametrize("connector_id", ["pi", "opencode"])
async def test_original_browser_native_verdict_through_real_harness(native_engine, connector_id):
    await run_native_mcpmark_case(native_engine, connector_id, service="playwright")


async def run_native_mcpmark_case(native_engine, connector_id, *, service):
    from evalhive_mcpmark.installation import install
    from evalhive_mcpmark.worker import MANIFEST

    postgres = service == "postgres"
    browser = service == "playwright"
    expected_calls, expected_tools = (3, 2) if postgres or browser else (5, 9)
    local = Path(os.environ["EVALHIVE_NATIVE_FIXTURE_DIR"]) / (
        ("mcpmark-" + service + "-") + connector_id
    )
    local.mkdir(parents=True)
    host_root = os.environ["EVALHIVE_NATIVE_FIXTURE_HOST"] + "/" + local.name
    client = docker.DockerClient(base_url="unix:///run/user/11001/docker.sock", timeout=60)
    worker_image = client.images.get(
        os.getenv("EVALHIVE_NATIVE_MCPMARK_WORKER_IMAGE", "evalhive-benchmark-mcpmark:0.1.0")
    )
    task_image = client.images.get(
        os.getenv(
            "EVALHIVE_NATIVE_MCPMARK_BROWSER_TASK_IMAGE", "evalhive-task-mcpmark-browser:0.1.0"
        )
        if browser
        else os.getenv("EVALHIVE_NATIVE_MCPMARK_TASK_IMAGE", "evalhive-task-mcpmark:0.1.0")
    )
    for image, repository in (
        (worker_image, "evalhive-benchmark-mcpmark"),
        (task_image, "evalhive-task-mcpmark"),
    ):
        assert image.tag(repository, "retained-" + image.id.removeprefix("sha256:")[:12])
    installed = local / "installed"
    initial = Path(
        os.environ[
            "EVALHIVE_MCPMARK_POSTGRES_DATA" if postgres else "EVALHIVE_MCPMARK_FILESYSTEM_DATA"
        ]
    )
    if browser:
        initial = local / "empty-initial"
        initial.mkdir()
    service_settings = (
        {"postgres_image": client.images.get("pgvector/pgvector:0.8.0-pg17-bookworm").id}
        if postgres
        else {
            "world_image": client.images.get(
                os.getenv(
                    "EVALHIVE_NATIVE_MCPMARK_WORLD_IMAGE", "evalhive-world-mcpmark-browser:0.1.0"
                )
            ).id,
            "service_settings": {"browser": "chromium", "headless": True},
        }
        if browser
        else {"categories": ["file_context"]}
    )
    receipt = install(
        os.environ["EVALHIVE_MCPMARK_NATIVE_ROOT"],
        installed,
        initial_root=initial,
        service=service,
        **service_settings,
        suite="standard" if browser else "easy",
        revision="native-verified-cd45b7f5",
        worker_image=worker_image.id,
        task_image=task_image.id,
    )
    selected = next(
        case
        for case in receipt["cases"]
        if case["native_ref"]["task_id"]
        == ("create_payment_index" if postgres else "extraction_table" if browser else "uppercase")
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
            title="Native MCPMark engineering acceptance",
            workspace_id=WORKSPACE,
            status="in_progress",
        )
        db.add(task)
        await db.flush()
        task_id = task.id
        await db.commit()
    store = TrialStore(engine, PrivateStore("native-mcpmark-fixture-key"))
    trial = await store.create(
        WORKSPACE,
        task_id=task_id,
        snapshot={"fixture": "native MCPMark"},
        deadline_at=datetime.now(UTC) + timedelta(seconds=600 if postgres or browser else 420),
    )
    owner = await store.claim(WORKSPACE, trial.id, uuid.uuid4())
    objects = Minio(
        os.environ["EVALHIVE_EVIDENCE_MINIO"],
        access_key="fixture-evidence",
        secret_key="fixture-evidence-password",
        secure=False,
        http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=3, read=10), retries=False),
    )
    bucket = "mcpmark-engineering-" + uuid.uuid4().hex
    await asyncio.to_thread(objects.make_bucket, bucket)
    evidence = EvidenceStore(store, objects, bucket=bucket)

    class Capture(FixtureArtifacts):
        async def save(self, scope, grant, chunks, *, publication=None):
            data = bytearray()

            async def observed():
                async for chunk in chunks:
                    data.extend(chunk)
                    yield chunk

            result = await evidence.sink(owner).save(
                scope, grant, observed(), publication=publication
            )
            self.values[grant.artifact_id] = bytes(data)
            return result

    artifacts = Capture()
    facade = SimpleNamespace(
        store=store,
        sink=lambda selected_owner: artifacts,
        validate_capture=evidence.validate_capture,
        open_verified=evidence.open_verified,
    )
    coordinator = RootlessCoordinator(
        runtime,
        {**manifest(), "id": "evalhive.rootless"},
        facade,
        limits=DriverLimits(
            phase_seconds=600 if postgres or browser else 240,
            capture_seconds=90,
            cleanup_seconds=60,
            stop_seconds=10,
            service_seconds=15,
        ),
        runner_policy=RunnerPolicy(heartbeat_seconds=0.2, startup_seconds=45, command_seconds=30),
    )
    host = None
    key = uuid.uuid4().hex
    db_password = uuid.uuid4().hex
    initial_files = (
        {}
        if postgres or browser
        else {
            f"file_{index:02d}.txt": (
                initial / "file_context" / f"file_{index:02d}.txt"
            ).read_text()
            for index in range(1, 11)
        }
    )
    try:
        with (
            MCPMarkPostgresEndpoint(key)
            if postgres
            else MCPMarkBrowserEndpoint(key)
            if browser
            else MCPMarkModelEndpoint(key, initial_files)
        ) as endpoint:
            harness_spec = {
                "protocol_version": "1.0",
                "workspace_id": str(WORKSPACE),
                "task_id": str(task_id),
                "attempt_id": str(uuid.uuid4()),
                "task": {"prompt": "Bound by native prepared access"},
                "cwd": "/workspace",
                "config": {},
                "limits": {"max_turns": 8},
                "interaction": {"version": "1.0", "mode": "single_turn"},
                "model": {
                    "name": "fixture-mcpmark-pg"
                    if postgres
                    else "fixture-mcpmark-browser"
                    if browser
                    else "fixture-mcpmark",
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
            original_install_plan = host.install_plan

            async def observed_install_plan(*args, **kwargs):
                try:
                    return await original_install_plan(*args, **kwargs)
                except Exception:
                    (local / "engineering-plan-failure.txt").write_text(
                        traceback.format_exc()
                        .replace(key, "[redacted]")
                        .replace(db_password, "[redacted]")
                    )
                    raise

            host.install_plan = observed_install_plan
            original_capture = controller._capture

            async def observed_capture(channel):
                if host.environment is not None and "task" in host.environment.leases:
                    lease = host.environment.leases["task"][1]
                    try:
                        observation = await host.host_broker.handle(
                            host.request(
                                host.scope,
                                "exec",
                                handle=lease.handle,
                                request_id="engineering.resources",
                                payload={
                                    "argv": [
                                        "/usr/local/bin/python",
                                        "-c",
                                        "from pathlib import Path;import json;p=Path('/sys/fs/cgroup');d=Path('/tmp/evalhive-mcpmark-client.jsonl');print(json.dumps({'cgroup':{name:(p/name).read_text() for name in ['memory.events','memory.peak','cpu.stat'] if (p/name).is_file()},'client_states':d.read_text() if d.is_file() else None}))",
                                    ],
                                    "timeout_seconds": 10,
                                    "deadline_policy": "inherit",
                                    "process_policy": "stop_after_command",
                                },
                            )
                        )
                    except Exception as error:
                        observation = {
                            "exception": type(error).__name__,
                            "code": getattr(error, "code", None),
                        }
                    (local / "engineering-resource-observation.json").write_text(
                        json.dumps(observation, indent=2)
                    )
                return await original_capture(channel)

            controller._capture = observed_capture
            spec = {
                "protocol_version": "1.0",
                "scope": {
                    "workspace_id": str(WORKSPACE),
                    "trial_id": str(trial.id),
                    "generation": owner.generation,
                },
                "case": selected,
                "config": {"prepare_timeout_seconds": 600} if postgres else {},
                "credential_refs": {"service_credentials": "fixture-private-reference"}
                if postgres
                else {},
                "models": {},
                "limits": {
                    "wall_time_seconds": 600 if postgres or browser else 420,
                    "memory_mb": 3072 if postgres or browser else 2048,
                    "cpu_cores": 2.5 if postgres or browser else 2,
                    "disk_mb": 4096 if postgres else 2048,
                    "model_requests": 8,
                },
            }
            private_inputs = (
                {
                    "service_credentials": json.dumps(
                        {"service": "postgres", "password": db_password}
                    ).encode()
                }
                if postgres
                else {}
            )
            result = await asyncio.wait_for(
                controller.run(spec, private_inputs=private_inputs),
                840 if postgres or browser else 660,
            )
            (local / "engineering-result.json").write_text(json.dumps(result, indent=2))
            (local / "engineering-model-requests.json").write_text(
                json.dumps(endpoint.requests, indent=2)
                .replace(key, "[redacted]")
                .replace(db_password, "[redacted]")
            )
            async with AsyncSession(engine) as db:
                attempt = await db.get(ExecutionAttempt, host.agent_gateway.calls.session_id)
                if attempt is not None:
                    (local / "engineering-attempt.json").write_text(
                        json.dumps(
                            {
                                "status": attempt.status,
                                "result": attempt.result,
                                "completeness": attempt.completeness,
                            },
                            indent=2,
                        )
                        .replace(key, "[redacted]")
                        .replace(db_password, "[redacted]")
                    )
                    observed_events = list(
                        await db.scalars(
                            select(ExecutionEvent.payload)
                            .where(ExecutionEvent.attempt_id == attempt.id)
                            .order_by(ExecutionEvent.seq)
                        )
                    )
                    (local / "engineering-events.json").write_text(
                        json.dumps(observed_events, indent=2)
                        .replace(key, "[redacted]")
                        .replace(db_password, "[redacted]")
                    )
            evidence_dir = local / "engineering-evidence"
            evidence_dir.mkdir()
            for name, data in artifacts.values.items():
                (evidence_dir / (name + ".tar")).write_bytes(data)
            if result.get("execution") != "success" or result.get("evaluation") != "completed":
                async with AsyncSession(engine) as db:
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
                    diagnostics = {
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
                    }
                    (local / "engineering-diagnostics.json").write_text(
                        json.dumps(diagnostics, indent=2)
                        .replace(key, "[redacted]")
                        .replace(db_password, "[redacted]")
                    )
            assert (
                result.get("execution") == "success" and result.get("evaluation") == "completed"
            ), result
            assert result["recording"] == "complete" and result["cleanup"] == "complete", result
            assert len(endpoint.requests) == expected_calls and not endpoint.errors, endpoint.errors
            async with AsyncSession(engine) as db:
                attempt = await db.get(ExecutionAttempt, host.agent_gateway.calls.session_id)
                assert attempt.status == "success" and attempt.connector_id == connector_id
                events = list(
                    await db.scalars(
                        select(ExecutionEvent.payload).where(
                            ExecutionEvent.attempt_id == attempt.id
                        )
                    )
                )
                (local / "engineering-events.json").write_text(
                    json.dumps(events, indent=2)
                    .replace(key, "[redacted]")
                    .replace(db_password, "[redacted]")
                )
                tools = [event for event in events if event["event"] == "tool.end"]
                assert len(tools) == expected_tools and all(
                    event["data"]["status"] == "completed" for event in tools
                )
                assert key not in json.dumps(events)
                assert db_password not in json.dumps(events)
                evaluation = await db.scalar(
                    select(BenchmarkEvaluationAttempt)
                    .where(BenchmarkEvaluationAttempt.trial_id == trial.id)
                    .options(undefer(BenchmarkEvaluationAttempt.private_result))
                )
                graded = store.private.open(
                    "evaluation-result:" + str(evaluation.id), evaluation.private_result
                )
                assert (
                    graded["success"] is True and graded["completeness"]["native_verdict"] is True
                )
                leases = list(
                    await db.scalars(
                        select(BenchmarkResourceLease).where(
                            BenchmarkResourceLease.trial_id == trial.id
                        )
                    )
                )
                assert leases and all(lease.state == "released" for lease in leases)
            with tarfile.open(fileobj=io.BytesIO(artifacts.values["report"])) as archive:
                verdict = json.loads(archive.extractfile("report/verdict.json").read())
            assert (
                verdict["native_result"]["success"] is True and verdict["verifier_exit_code"] == 0
            )
            if postgres:
                assert (
                    "evalhive_engineering_customer_id"
                    in verdict["native_result"]["verification_output"]
                )
                assert db_password not in json.dumps(verdict)
                assert all("Error:" not in json.dumps(event["data"]["result"]) for event in tools)
            elif not browser:
                with tarfile.open(fileobj=io.BytesIO(artifacts.values["working"])) as archive:
                    files = [member.name for member in archive.getmembers()]
                    assert (
                        len(
                            [
                                name
                                for name in files
                                if "/uppercase/" in name and name.endswith(".txt")
                            ]
                        )
                        == 6
                    )
            usage = await trial_usage(store, WORKSPACE, trial.id)
            (local / "engineering-usage.json").write_text(json.dumps(usage, indent=2))
            assert usage["valid"] and all(group["role"] == "agent" for group in usage["groups"])
            observed_requests = {
                (event["source"], event.get("session_id"), event["data"]["usage_id"])
                for event in events
                if event["event"] == "usage"
            }
            failed_requests = {
                event["data"]["payload"]["request_id"]
                for event in events
                if event["event"] == "native"
                and event["data"].get("name") == "provider.response"
                and event["data"]["payload"].get("status", 0) >= 400
            }
            calls = sum(group["calls"] for group in usage["groups"])
            assert calls == len(observed_requests) == expected_calls + len(failed_requests)
            assert (
                sum(group["tokens"]["input_tokens"]["known_sum"] for group in usage["groups"])
                == 10 * expected_calls
            )
            assert (
                sum(group["tokens"]["output_tokens"]["known_sum"] for group in usage["groups"])
                == 2 * expected_calls
            )
            assert sum(
                group["tokens"]["input_tokens"]["unknown_calls"] for group in usage["groups"]
            ) == len(failed_requests)
    finally:
        if host is not None:
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
            await host.host_broker.close()
            await host.worker_broker.close()
        client.close()
