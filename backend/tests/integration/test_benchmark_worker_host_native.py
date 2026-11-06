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

import docker
import pytest
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
from app.runtime.managed_docker import RootlessDockerRuntime
from app.runtime.managed_spec import PathMapping, Quota, RuntimePolicy
from app.runtime.reconcile import RootlessReconciler
from app.runtime.registered_assets import RegisteredFile, RootlessAssets
from evalhive_benchmark import ProtocolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from tests.runtime.authority_fixture import FixtureArtifacts, manifest
from tests.runtime.broker_worker_fixture import CASE, Worker

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_WORKER_HOST") != "1",
    reason="Requires explicit isolated worker/controller checks",
)
WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000002")


async def test_real_worker_plans_prepares_executes_and_captures_via_durable_broker(
    _engine, db_session
):
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
    worker_image = await asyncio.to_thread(
        runtime.resolve_image, "evalhive-broker-worker-fixture:b04"
    )
    task_image = await asyncio.to_thread(
        runtime.resolve_image, "evalhive-environment-workspace:0.1.0"
    )
    declaration = deepcopy(Worker.manifest)
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
    assets = RootlessAssets(
        registration,
        runtime,
        files={
            "supervisor": RegisteredFile(
                host_root + "/bundle",
                "sha256:" + hashlib.sha256((bundle / "supervisor").read_bytes()).hexdigest(),
            )
        },
        connector_id=declaration["id"],
        connector_version=declaration["version"],
    )
    store = TrialStore(_engine, PrivateStore("native-worker-host-fixture-key"))
    trial = await store.create(
        WORKSPACE,
        snapshot={"fixture": "native worker host"},
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    owner = await store.claim(WORKSPACE, trial.id, uuid.uuid4())
    await store.transition(owner, "created", "planning")
    artifacts = FixtureArtifacts()
    factories = {
        "fixture.rootless": lambda authority: RootlessDockerProvider(
            runtime, authority, manifest(), artifacts=artifacts
        )
    }
    worker_host = RootlessWorkerHost(store, owner, declaration, assets, factories)
    spec = {
        "protocol_version": "1.0",
        "scope": {
            "workspace_id": str(owner.workspace_id),
            "trial_id": str(owner.trial_id),
            "generation": owner.generation,
        },
        "case": CASE,
        "config": {},
        "credential_refs": {},
        "models": {},
        "limits": {"wall_time_seconds": 60, "memory_mb": 768, "cpu_cores": 1.25, "disk_mb": 32},
    }
    try:
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
        candidates = [
            (
                worker_host.scope,
                str(
                    uuid.uuid5(
                        uuid.UUID(worker_host.scope.session_id), "resource:" + worker_host.WORKER_ID
                    )
                ),
            )
        ]
        if worker_host.environment is not None:
            candidates += [
                (worker_host.environment.scope, lease.handle)
                for _, lease in worker_host.environment.leases.values()
                if lease.kind == "container"
            ]
        for selected_scope, handle in candidates:
            with suppress(Exception):
                native = await asyncio.to_thread(runtime.find, selected_scope, handle)
                if native is not None:
                    await asyncio.to_thread(runtime.stop, native, grace_seconds=0, remove=True)
        if worker_host.channel is not None:
            with suppress(Exception):
                await worker_host.channel.close(grace=0.2)
        await worker_host.host_broker.close()
        await worker_host.worker_broker.close()
        docker_client.close()
