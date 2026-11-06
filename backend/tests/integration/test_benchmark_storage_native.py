"""A durable native workspace is shared without exposing evaluator gold to Task."""

import asyncio
import io
import os
import tarfile
import uuid
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import docker
import pytest
from app.benchmarks.private_store import PrivateStore
from app.benchmarks.recovery import RecoveryService
from app.benchmarks.resource_authority import ActorPolicy, SqlResourceAuthority, install_session
from app.benchmarks.trials import TrialStore
from app.models.benchmark_trial import BenchmarkHostReservation, BenchmarkTrial
from app.runtime.admission import GroupPolicy, host_capacity
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.managed_docker import RootlessDockerRuntime
from app.runtime.managed_spec import OwnerScope, PathMapping, Quota, RuntimePolicy
from app.runtime.managed_storage import RootlessVolumeRuntime
from app.runtime.reconcile import RootlessReconciler
from app.runtime.registered_assets import RegisteredFile, RootlessAssets
from evalhive_benchmark import ProtocolError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.runtime.authority_fixture import FixtureArtifacts, manifest

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_STORAGE") != "1",
    reason="Requires explicit isolated owned storage acceptance",
)
WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000002")
PROVIDER = "fixture.rootless"


@pytest.mark.parametrize("lose_create_reply", [False, True])
async def test_native_storage_reservation_gold_boundary_and_cold_cleanup(
    _engine, db_session, lose_create_reply
):
    local = Path(os.environ["EVALHIVE_NATIVE_FIXTURE_DIR"])
    host_root = os.environ["EVALHIVE_NATIVE_FIXTURE_HOST"]
    gold = local / "private-gold"
    gold.mkdir(exist_ok=True)
    (gold / "answer.txt").write_text("native-answer")
    client = docker.DockerClient(base_url="unix:///run/user/11001/docker.sock", timeout=15)
    runtime = RootlessDockerRuntime(
        client,
        RuntimePolicy(
            PathMapping(host_root, str(local)),
            (PathMapping(host_root + "/private-gold", str(gold)),),
        ),
    )
    storage = RootlessVolumeRuntime(runtime)
    image = await asyncio.to_thread(runtime.resolve_image, "evalhive-storage-fixture:b08")
    store = TrialStore(_engine, PrivateStore("native-owned-workspace-fixture-key"))
    trial = await store.create(
        WORKSPACE, snapshot={}, deadline_at=datetime.now(UTC) + timedelta(seconds=90)
    )
    owner = await store.claim(WORKSPACE, trial.id, uuid.uuid4())
    await store.transition(owner, "created", "planning")
    scope = OwnerScope(str(WORKSPACE), str(trial.id), str(uuid.uuid4()), owner.generation)
    container = {
        "kind": "container",
        "image": image["image_id"],
        "role": "environment",
        "limits": asdict(Quota(128, 0.25, 64)),
        "minimum": {"memory_mb": 128, "cpu_cores": 0.25, "disk_mb": 16},
        "storage_slots": {"workspace": {"target": "/workspace", "read_only": False}},
    }
    registration = {
        "format_version": "1.0",
        "connector_id": "fixture.storage",
        "connector_version": "1.0",
        "provider_id": PROVIDER,
        "worker_asset": "worker",
        "assets": {
            "worker": {**deepcopy(container), "role": "worker", "storage_slots": {}},
            "files": {"kind": "volume", "disk_mb": 64},
            "task": {
                **deepcopy(container),
                "command": [
                    "python",
                    "-c",
                    "from pathlib import Path;import time;assert not Path('/private-gold').exists();assert not Path('/workspace/baked-in-gold.txt').exists();Path('/workspace/result.txt').write_text('native-answer');time.sleep(300)",
                ],
            },
            "grader": {
                **deepcopy(container),
                "role": "grader",
                "storage_slots": {"workspace": {"target": "/workspace", "read_only": True}},
                "mounts": [{"file_id": "gold", "target": "/private-gold", "read_only": True}],
                "command": [
                    "python",
                    "-c",
                    "from pathlib import Path;import time;p=Path('/workspace/result.txt');end=time.monotonic()+5\nwhile not p.exists() and time.monotonic()<end: time.sleep(.01)\nassert p.read_text()==Path('/private-gold/answer.txt').read_text();Path('/tmp/verdict.txt').write_text('passed');time.sleep(300)",
                ],
                "outputs": {
                    "report": {
                        "path": "/tmp/verdict.txt",
                        "visibility": "evaluator",
                        "required": True,
                        "max_archive_bytes": 1024**2,
                    }
                },
            },
        },
    }
    assets = RootlessAssets(
        registration,
        runtime,
        files={"gold": RegisteredFile(host_root + "/private-gold", "sha256:" + "a" * 64)},
        connector_id="fixture.storage",
        connector_version="1.0",
    )
    plan = {
        "resources": [
            {
                "id": "files",
                "kind": "volume",
                "provider": PROVIDER,
                "config": {"asset_id": "files"},
            },
            {
                "id": "task",
                "kind": "container",
                "provider": PROVIDER,
                "depends_on": ["files"],
                "config": {"asset_id": "task", "storage": {"workspace": "files"}},
            },
            {
                "id": "grader",
                "kind": "container",
                "provider": PROVIDER,
                "depends_on": ["files", "task"],
                "config": {"asset_id": "grader", "storage": {"workspace": "files"}},
            },
        ],
        "agent": {
            "mode": "in_environment",
            "interaction": "autonomous",
            "environment_handle": "task",
            "cwd": "/workspace",
            "prompt": "Fixture",
            "instructions": [],
            "resources": [],
        },
        "outputs": [
            {
                "id": "report",
                "resource_id": "grader",
                "path": "/tmp/verdict.txt",
                "visibility": "evaluator",
                "required": True,
            }
        ],
        "requirements": {},
        "grading_mode": "container_native",
    }
    compiled = await assets.compile(scope, plan)
    actor = uuid.uuid4()
    artifacts = FixtureArtifacts()
    authority = SqlResourceAuthority(store, owner, scope.session_id, actor, PROVIDER)
    provider = RootlessDockerProvider(runtime, authority, manifest(), artifacts=artifacts)

    def request(operation, *, handle=None, payload=None):
        frame = {
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
            "payload": payload or {},
        }
        if handle:
            frame["handle"] = handle
        return frame

    try:

        async def measure():
            return await asyncio.to_thread(host_capacity, runtime, local)

        budget = await store.reserve(
            owner,
            demands=compiled.demands,
            native_resources={},
            measure=measure,
            runtime_policy=runtime.policy,
            group_policy=GroupPolicy(),
        )
        assert budget["group_resources"] == {"memory_mb": 256, "cpu_cores": 0.5, "disk_mb": 96}
        await install_session(
            store,
            owner,
            scope,
            leases=compiled.leases,
            actors={
                actor: ActorPolicy(
                    (PROVIDER,),
                    tuple(compiled.leases),
                    ("planning", "cleaning"),
                    ("allocate", "inspect", "export", "release"),
                )
            },
            outputs=compiled.output_grants([actor]),
            links=compiled.links,
        )
        volume = compiled.leases["files"][1]
        if lose_create_reply:
            allocation = request("allocate", payload={"resource_id": "files"})
            with pytest.raises(ProtocolError):
                async with authority.operation(allocation) as call:
                    planned = await call.allocation("files")
                    await call.save(replace(planned, state="creating"))
                    await asyncio.to_thread(storage.create, planned.intent)
                    raise ProtocolError(
                        "RESOURCE_UNCERTAIN", "Fixture lost storage receipt", "uncertain"
                    )
        else:
            await provider.handle(request("allocate", payload={"resource_id": "files"}))
            for logical_id in ("task", "grader"):
                await provider.handle(request("allocate", payload={"resource_id": logical_id}))
            grader = await asyncio.to_thread(
                runtime.find, scope, compiled.leases["grader"][1].handle
            )
            native = await asyncio.to_thread(runtime.container, grader)
            async with asyncio.timeout(5):
                while True:
                    from docker.errors import NotFound

                    try:
                        chunks, _ = await asyncio.to_thread(native.get_archive, "/tmp/verdict.txt")
                        with tarfile.open(
                            fileobj=io.BytesIO(await asyncio.to_thread(b"".join, chunks))
                        ) as archive:
                            assert archive.extractfile("verdict.txt").read() == b"passed"
                        break
                    except NotFound:
                        await asyncio.sleep(0.05)
            await provider.handle(
                request("export", handle=grader.resource_id, payload={"output_id": "report"})
            )
            assert artifacts.values["report"]
            with pytest.raises(ProtocolError) as attached:
                await provider.handle(request("release", handle=volume.handle))
            assert attached.value.code == "RESOURCE_UNCERTAIN"
        # A fresh controller has only durable intents and native ownership labels.
        async with _engine.begin() as connection:
            await connection.execute(
                update(BenchmarkTrial)
                .where(BenchmarkTrial.id == trial.id)
                .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        recovered = await store.claim(WORKSPACE, trial.id, uuid.uuid4())
        recovery = RecoveryService(
            store, actor, {PROVIDER: lambda value: RootlessReconciler(runtime, value, PROVIDER)}
        )
        released = await recovery.sweep(recovered, "release")
        assert released and all(item.get("state") == "released" for item in released), released
        assert await asyncio.to_thread(storage.find, scope, volume.handle) is None
        await store.transition(recovered, "cleaning", "finished")
        async with AsyncSession(_engine) as db:
            assert await db.get(BenchmarkHostReservation, store.host_id) is None
    finally:
        for _, lease in reversed(tuple(compiled.leases.values())):
            if lease.kind == "container":
                with suppress(Exception):
                    native = await asyncio.to_thread(runtime.find, scope, lease.handle)
                    if native:
                        await asyncio.to_thread(runtime.stop, native, grace_seconds=0, remove=True)
        with suppress(Exception):
            native = await asyncio.to_thread(
                storage.find, scope, compiled.leases["files"][1].handle
            )
            if native:
                await asyncio.to_thread(storage.remove, native)
        await provider.close()
        client.close()
