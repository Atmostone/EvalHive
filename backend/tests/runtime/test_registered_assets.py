"""Untrusted plans select server grants without acquiring image/path authority."""

import asyncio
import copy
import uuid
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from app.runtime.managed_spec import (
    ManagedRuntimeError,
    OwnerScope,
    PathMapping,
    Quota,
    RuntimePolicy,
)
from app.runtime.registered_assets import RegisteredFile, RootlessAssets
from evalhive_benchmark import ProtocolError

IMAGE = "sha256:" + "a" * 64
WORKER_IMAGE = "sha256:" + "b" * 64
QUOTA = Quota(256, 0.25, 128)
POLICY = RuntimePolicy(PathMapping("/owned", "/owned"))
OUTPUT = {
    "path": "/workspace/result.txt",
    "visibility": "user",
    "max_archive_bytes": 1048576,
    "required": True,
}


def scope():
    return OwnerScope(*(str(uuid.uuid4()) for _ in range(3)), 1)


def document():
    container = {
        "kind": "container",
        "image": IMAGE,
        "role": "environment",
        "limits": asdict(QUOTA),
        "minimum": {"memory_mb": 256, "cpu_cores": 0.25, "disk_mb": 16},
    }
    return {
        "format_version": "1.0",
        "connector_id": "fixture.benchmark",
        "connector_version": "1.0",
        "provider_id": "fixture.rootless",
        "worker_asset": "fixture.worker",
        "assets": {
            "fixture.worker": {**copy.deepcopy(container), "image": WORKER_IMAGE, "role": "worker"},
            "fixture.task": {**copy.deepcopy(container), "outputs": {"result": dict(OUTPUT)}},
            "fixture.network": {"kind": "network", "egress": False},
            "fixture.service": {**copy.deepcopy(container), "role": "service"},
        },
    }


def plan():
    return {
        "resources": [
            {
                "id": "task",
                "kind": "container",
                "provider": "fixture.rootless",
                "config": {"asset_id": "fixture.task"},
            }
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
                "id": "result",
                "resource_id": "task",
                "path": OUTPUT["path"],
                "visibility": "user",
                "required": True,
            }
        ],
        "requirements": {},
        "grading_mode": "environment",
    }


def catalog(data=None, files=None):
    reads = []

    def resolve(image):
        reads.append(image)
        return {"image_id": image}

    runtime = SimpleNamespace(policy=POLICY, resolve_image=resolve)
    assets = RootlessAssets(
        data or document(),
        runtime,
        files=files,
        connector_id="fixture.benchmark",
        connector_version="1.0",
    )
    return assets, reads


def test_shared_storage_has_registered_slots_dependencies_and_a_disk_only_reservation():
    async def scenario():
        from app.runtime.admission import StorageDemand

        data = document()
        data["assets"]["fixture.storage"] = {"kind": "volume", "disk_mb": 64}
        data["assets"]["fixture.task"]["storage_slots"] = {
            "workspace": {"target": "/workspace", "read_only": False}
        }
        native = plan()
        native["resources"].insert(
            0,
            {
                "id": "files",
                "kind": "volume",
                "provider": "fixture.rootless",
                "config": {"asset_id": "fixture.storage"},
            },
        )
        native["resources"][1].update(depends_on=["files"])
        native["resources"][1]["config"]["storage"] = {"workspace": "files"}
        assets, _ = catalog(data)
        compiled = await assets.compile(scope(), native)
        assert compiled.demands[0] == StorageDemand("files", 64)
        assert compiled.images == {"task": IMAGE}
        assert compiled.links["task"]["storage"] == [
            {"resource": "files", "target": "/workspace", "read_only": False}
        ]
        assert compiled.leases["task"][1].dependencies == (compiled.leases["files"][1].handle,)
        native["resources"][1]["config"]["storage"] = {"unknown": "files"}
        with pytest.raises(ManagedRuntimeError):
            await assets.compile(scope(), native)
        native["resources"][1]["config"]["storage"] = {"workspace": "files"}
        native["resources"][1]["depends_on"] = []
        with pytest.raises(ManagedRuntimeError):
            await assets.compile(scope(), native)

    asyncio.run(scenario())


def test_compile_freezes_native_process_defaults_images_requirements_and_outputs():
    async def scenario():
        assets, _ = catalog()
        origin = scope()
        compiled = await assets.compile(origin, plan())
        lease = compiled.leases["task"][1]
        assert lease.scope == origin and lease.intent.image_id == IMAGE
        assert lease.intent.entrypoint is None and lease.intent.command is None
        assert lease.intent.user is None and lease.intent.workdir is None
        assert compiled.demands[0].minimum_memory_mb == 256
        assert compiled.images == {"task": IMAGE}
        assert (await assets.compile(origin, plan())).fingerprint == compiled.fingerprint
        assert (await assets.compile(origin, plan())).leases["task"][1].handle == lease.handle
        assert (
            compiled.output_grants(["actor"])["task"]["result"]["grant"]["path"] == OUTPUT["path"]
        )

    asyncio.run(scenario())


def test_evaluation_plan_compiles_without_an_agent_grant():
    async def scenario():
        assets, _ = catalog()
        native = plan()
        del native["agent"]
        native["purpose"] = "evaluation"
        compiled = await assets.compile(scope(), native)
        assert tuple(compiled.leases) == ("task",)
        assert compiled.demands[0].minimum_memory_mb == 256
        assert compiled.outputs["task"]["result"].path == OUTPUT["path"]
        native["agent"] = plan()["agent"]
        with pytest.raises(ProtocolError):
            await assets.compile(scope(), native)

    asyncio.run(scenario())


def test_retained_grants_have_fixed_readonly_targets_and_enter_the_fingerprint():
    async def scenario():
        assets, _ = catalog()
        native = plan()
        native["resources"][0]["config"]["retained_inputs"] = ["checkpoint"]
        grant = RegisteredFile("/owned/checkpoint", "sha256:" + "c" * 64)
        compiled = await assets.compile(scope(), native, retained_inputs={"checkpoint": grant})
        (mount,) = compiled.leases["task"][1].intent.mounts
        assert mount.source == "/owned/checkpoint" and mount.read_only
        assert mount.target == "/evalhive/retained/checkpoint/payload"
        assert compiled.files["retained/checkpoint"] == grant.source_digest
        changed = RegisteredFile(grant.host_path, "sha256:" + "d" * 64)
        second = await assets.compile(
            compiled.scope, native, retained_inputs={"checkpoint": changed}
        )
        assert second.fingerprint != compiled.fingerprint
        with pytest.raises(ManagedRuntimeError, match="authorized"):
            await assets.compile(scope(), native)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "selection", [["checkpoint", "checkpoint"], ["foreign"], [1], "checkpoint"]
)
def test_retained_selection_cannot_create_or_repeat_grants(selection):
    async def scenario():
        assets, _ = catalog()
        native = plan()
        native["resources"][0]["config"]["retained_inputs"] = selection
        with pytest.raises(ManagedRuntimeError):
            await assets.compile(
                scope(),
                native,
                retained_inputs={
                    "checkpoint": RegisteredFile("/owned/checkpoint", "sha256:" + "c" * 64)
                },
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "source,target,writable",
    [
        ("/owned/checkpoint", "/seed", False),
        ("/owned", "/seed", False),
        ("/owned/seed", "/evalhive", False),
        ("/owned/seed", "/seed", True),
    ],
)
def test_retained_inputs_cannot_overlap_mounts_or_be_writable(source, target, writable):
    async def scenario():
        data = document()
        data["assets"]["fixture.task"]["mounts"] = [
            {"file_id": "seed", "target": target, "read_only": True}
        ]
        assets, _ = catalog(data, {"seed": RegisteredFile(source, "sha256:" + "a" * 64)})
        native = plan()
        native["resources"][0]["config"]["retained_inputs"] = ["checkpoint"]
        with pytest.raises(ManagedRuntimeError):
            await assets.compile(
                scope(),
                native,
                retained_inputs={
                    "checkpoint": RegisteredFile(
                        "/owned/checkpoint", "sha256:" + "c" * 64, writable
                    )
                },
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "config",
    [
        {"asset_id": "missing"},
        {"asset_id": {}},
        {"asset_id": []},
        {"image": IMAGE},
        {"asset_id": "fixture.task", "image": IMAGE},
        {"asset_id": "fixture.task", "mounts": []},
        {"asset_id": "fixture.task", "limits": {}},
        {"asset_id": "fixture.task", "command": ["bash"]},
        {"asset_id": "fixture.task", "network": "foreign"},
        {"asset_id": "fixture.task", "namespace": "foreign"},
        {"asset_id": "fixture.task", "network_aliases": ["foreign"]},
        {"asset_id": "fixture.worker"},
    ],
)
def test_plan_cannot_grant_itself_resources(config):
    async def scenario():
        assets, _ = catalog()
        value = plan()
        value["resources"][0]["config"] = config
        with pytest.raises((ManagedRuntimeError, ProtocolError)):
            await assets.compile(scope(), value)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "/tests/gold.json"),
        ("visibility", "private"),
        ("required", False),
        ("stage", "evaluation"),
    ],
)
def test_output_paths_visibility_and_completeness_cannot_be_changed(field, value):
    async def scenario():
        assets, _ = catalog()
        changed = plan()
        changed["outputs"][0][field] = value
        with pytest.raises(ManagedRuntimeError):
            await assets.compile(scope(), changed)

    asyncio.run(scenario())


def test_required_output_cannot_be_omitted():
    async def scenario():
        assets, _ = catalog()
        changed = plan()
        changed["outputs"] = []
        with pytest.raises(ManagedRuntimeError):
            await assets.compile(scope(), changed)

    asyncio.run(scenario())


def test_output_stage_is_registered_and_participates_in_fingerprint():
    async def scenario():
        registered = document()
        registered["assets"]["fixture.task"]["outputs"]["result"]["stage"] = "evaluation"
        assets, _ = catalog(registered)
        declared = plan()
        with pytest.raises(ManagedRuntimeError):
            await assets.compile(scope(), declared)
        declared["outputs"][0]["stage"] = "evaluation"
        origin = scope()
        compiled = await assets.compile(origin, declared)
        normal, _ = catalog()
        assert compiled.fingerprint != (await normal.compile(origin, plan())).fingerprint

    asyncio.run(scenario())


@pytest.mark.parametrize("writable", [False, True])
def test_writable_mount_requires_a_separate_server_file_grant(writable):
    async def scenario():
        registered = document()
        registered["assets"]["fixture.task"]["mounts"] = [
            {"file_id": "data", "target": "/workspace/input", "read_only": False}
        ]
        assets, _ = catalog(
            registered,
            {
                "data": RegisteredFile(
                    "/owned/benchmark-sessions/data", "sha256:" + "c" * 64, writable
                )
            },
        )
        if not writable:
            with pytest.raises(ManagedRuntimeError):
                await assets.compile(scope(), plan())
        else:
            result = await assets.compile(scope(), plan())
            assert not result.leases["task"][1].intent.mounts[0].read_only

    asyncio.run(scenario())


def test_graph_keeps_arbitrary_dependencies_and_registered_private_network_links():
    async def scenario():
        assets, _ = catalog()
        value = plan()
        value["resources"] = [
            {
                "id": "network",
                "kind": "network",
                "provider": "fixture.rootless",
                "config": {"asset_id": "fixture.network"},
            },
            {
                "id": "database",
                "kind": "container",
                "provider": "fixture.rootless",
                "config": {
                    "asset_id": "fixture.service",
                    "network": "network",
                    "network_aliases": ["database"],
                },
                "depends_on": ["network"],
            },
            {
                **value["resources"][0],
                "depends_on": ["database", "network"],
                "config": {"asset_id": "fixture.task", "network": "network"},
            },
        ]
        compiled = await assets.compile(scope(), value)
        task = compiled.leases["task"][1]
        assert set(task.dependencies) == {
            compiled.leases["database"][1].handle,
            compiled.leases["network"][1].handle,
        }
        assert compiled.links["task"] == {"network": "network"}
        assert compiled.leases["database"][1].intent.network_aliases == ("database",)
        assert not compiled.leases["network"][1].intent.egress

    asyncio.run(scenario())


def test_float_image_and_lowered_native_minima_are_rejected():
    value = document()
    value["assets"]["fixture.task"]["image"] = "fixture:latest"
    with pytest.raises(ManagedRuntimeError):
        catalog(value)
    value = document()
    value["assets"]["fixture.task"]["minimum"]["memory_mb"] = 512

    async def scenario():
        assets, reads = catalog(value)
        with pytest.raises(ManagedRuntimeError):
            await assets.compile(scope(), plan())
        assert not reads

    asyncio.run(scenario())


def test_registered_file_receipt_changes_the_effective_fingerprint():
    async def scenario():
        registered = document()
        registered["assets"]["fixture.task"]["mounts"] = [
            {"file_id": "helper", "target": "/evalhive-runtime", "read_only": True}
        ]
        origin = scope()
        original, _ = catalog(
            registered, {"helper": RegisteredFile("/owned/helper", "sha256:" + "c" * 64)}
        )
        changed, _ = catalog(
            registered, {"helper": RegisteredFile("/owned/helper", "sha256:" + "d" * 64)}
        )
        first = await original.compile(origin, plan())
        second = await changed.compile(origin, plan())
        assert first.fingerprint != second.fingerprint
        assert first.files == {"helper": "sha256:" + "c" * 64}

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["/", "//"])
def test_root_directory_cannot_be_exported_as_a_public_leaf_artifact(path):
    async def scenario():
        registered = document()
        registered["assets"]["fixture.task"]["outputs"]["result"]["path"] = path
        assets, _ = catalog(registered)
        value = plan()
        value["outputs"][0]["path"] = path
        with pytest.raises(ManagedRuntimeError):
            await assets.compile(scope(), value)

    asyncio.run(scenario())


@pytest.mark.parametrize("role", ["service", "grader"])
def test_agent_cannot_select_service_or_grader_as_its_native_task(role):
    async def scenario():
        registration = document()
        registration["assets"]["fixture.task"]["role"] = role
        native, _ = catalog(registration)
        with pytest.raises(ManagedRuntimeError, match="task container"):
            await native.compile(scope(), plan())

    asyncio.run(scenario())
