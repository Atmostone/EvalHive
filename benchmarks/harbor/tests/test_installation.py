"""Native installation snapshots preserve source bytes and explicit image budgets."""

import hashlib
import json

import pytest
from evalhive_benchmark import validate_environment
from evalhive_harbor.dataset import InstalledHarbor
from evalhive_harbor.installation import install
from evalhive_harbor.native import HarborConfigurationError
from evalhive_harbor.worker import HarborConnector
from test_native import task
from test_worker import SCOPE, Context

IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def tools(tmp_path, monkeypatch):
    root = tmp_path / "tools"
    root.mkdir()
    proof = {}
    for name in ("busybox", "ld-musl-x86_64.so.1"):
        content = ("Fixture for " + name).encode()
        (root / name).write_bytes(content)
        proof[name] = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr("evalhive_harbor.installation.TRANSFER_TOOLS", proof)
    return root


def metadata():
    return {
        "native-task": {
            "task": {
                "image": IMAGE,
                "image_user": "1000:1000",
                "image_workdir": "/image-workdir",
                "limits": {"memory_mb": 128, "cpu_cores": 0.25, "disk_mb": 64},
                "reserved_paths_checked": True,
            }
        }
    }


@pytest.mark.asyncio
async def test_native_service_installation_drives_one_scoped_group_with_native_dependencies(
    tmp_path, tools
):
    source = tmp_path / "source"
    root = task(
        source,
        '[environment]\nmemory_mb=128\ncpus=0.25\nstorage_mb=64\nnetwork_mode="public"\n[environment.healthcheck]\ncommand="printf native-ready"\n',
    )
    (root / "environment/docker-compose.yaml").write_text("""services:
  main:
    depends_on: [database]
    environment: {ORIGIN: "native-compose"}
    volumes: [shared:/workspace]
  database:
    image: native-database:fixture
    command: [python, -m, http.server, "8768"]
    working_dir: /data
    cpus: 0.1
    mem_limit: 64m
    volumes: [shared:/data]
volumes:
  shared: {}
""")
    images = metadata()
    images["native-task"]["services"] = {
        "task": {
            "database": {
                **metadata()["native-task"]["task"],
                "limits": {"memory_mb": 64, "cpu_cores": 0.1, "disk_mb": 64},
            }
        }
    }
    images["native-task"]["volumes"] = {"task": {"shared": 64}}
    output = tmp_path / "installed"
    receipt = install(
        source,
        output,
        revision="native-service-fixture",
        worker_image=IMAGE,
        image_bindings=images,
        transfer_tools=tools,
    )
    assets = json.loads((output / "resources.json").read_text())["assets"]
    bindings = json.loads((output / "worker-data/runtime.json").read_text())["cases"][
        "native-task"
    ]["task"]
    assert bindings["order"] == ["database", "main"]
    assert assets[bindings["services"]["database"]["asset_id"]]["command"] == [
        "python",
        "-m",
        "http.server",
        "8768",
    ]
    assert assets[bindings["asset_id"]]["environment"] == {"ORIGIN": "native-compose"}
    assert assets[bindings["volumes"]["shared"]] == {"kind": "volume", "disk_mb": 64}
    assert assets[bindings["asset_id"]]["storage_slots"]["native.shared"]["no_copy"] is False
    worker = HarborConnector(output / "worker-data")
    spec = {
        "protocol_version": "1.0",
        "scope": SCOPE,
        "case": receipt["cases"][0],
        "config": {},
        "credential_refs": {},
        "models": {},
        "limits": {"wall_time_seconds": 60, "memory_mb": 1024, "cpu_cores": 1, "disk_mb": 1536},
    }
    context = Context()
    plan = await worker.handle("plan", {"spec": spec}, context)
    validate_environment(spec, plan)
    task_plan = next(item for item in plan["resources"] if item["id"] == "task")
    database = next(item for item in plan["resources"] if item["id"] == "task.service.database")
    assert "task.service.database" in task_plan["depends_on"]
    assert database["config"]["network_aliases"] == ["database"]
    assert database["config"]["storage"] == {
        "stage": "stage",
        "native.shared": "task.volume.shared",
    }
    prepared = await worker.handle("prepare", {"spec": spec, "plan": plan}, context)
    validate_environment(spec, prepared, kind="prepared")
    allocations = [
        payload["resource_id"] for operation, payload, _ in context.calls if operation == "allocate"
    ]
    assert allocations.index("task.service.database") < allocations.index("task")
    assert (
        worker._environment(context, "task").services["database"].handle
        == "owned-task.service.database"
    )
    await worker.close(context)


def test_installation_is_atomic_and_keeps_native_user_workdir_files_and_tool_grants(
    tmp_path, tools
):
    source = tmp_path / "source"
    task(
        source,
        '[environment]\nmemory_mb=128\ncpus=0.25\nstorage_mb=64\nworkdir="/native-workdir"\n[agent]\nuser="2000"\n',
    )
    output = tmp_path / "installed"
    receipt = install(
        source,
        output,
        revision="pinned",
        worker_image=IMAGE,
        image_bindings=metadata(),
        transfer_tools=tools,
    )
    assert InstalledHarbor(output / "worker-data/native", receipt).resolve(receipt["cases"][0])
    files = json.loads((output / "files.json").read_text())["files"]
    assert files["worker-data"]["path"] == "worker-data"
    assert files["harness-runtime"] == {"kind": "runtime_binding", "name": "harness.runtime"}
    assets = json.loads((output / "resources.json").read_text())["assets"]
    runtime = json.loads((output / "worker-data/runtime.json").read_text())
    binding = runtime["cases"]["native-task"]["task"]
    native = assets[binding["asset_id"]]
    assert native["workdir"] == "/native-workdir" and native["user"] == "1000:1000"
    assert native["agent_user"] == "2000"
    assert native["agent_process_policy"] == "retain_until_cleanup"
    assert native["storage_slots"]["stage"]["read_only"]
    assert {mount["file_id"] for mount in native["mounts"]} == {"harness-runtime", "transfer-tools"}
    assert "entrypoint" not in native  # Native image startup remains intact.
    worker = HarborConnector(output / "worker-data")
    assert worker.installed().receipt == receipt
    worker.temporary.cleanup()
    with pytest.raises(HarborConfigurationError, match="already exists"):
        install(
            source,
            output,
            revision="pinned",
            worker_image=IMAGE,
            image_bindings=metadata(),
            transfer_tools=tools,
        )


@pytest.mark.parametrize(
    "change", ["image", "memory", "disk", "environment_template", "unknown", "toolchain"]
)
def test_invalid_native_binding_cannot_publish_a_partial_installation(tmp_path, tools, change):
    source = tmp_path / "source"
    task(
        source,
        "[environment]\nmemory_mb=128\nstorage_mb=64\n"
        + ('env={NATIVE_TOKEN="${OWNER_SECRET}"}\n' if change == "environment_template" else ""),
    )
    images = metadata()
    selected = images["native-task"]["task"]
    if change == "image":
        selected["image"] = "floating:latest"
    elif change == "memory":
        selected["limits"]["memory_mb"] = 64
    elif change == "disk":
        selected["limits"]["disk_mb"] = 32
    elif change == "unknown":
        selected["host_mount"] = "/outside"
    elif change == "toolchain":
        (tools / "busybox").write_text("Changed fixture tool")
    output = tmp_path / "installed"
    with pytest.raises(HarborConfigurationError):
        install(
            source,
            output,
            revision="pinned",
            worker_image=IMAGE,
            image_bindings=images,
            transfer_tools=tools,
        )
    assert not output.exists()
    assert not list(tmp_path.glob(".evalhive-harbor-*"))


@pytest.mark.parametrize("mode", ["shared", "separate"])
def test_every_container_running_broker_commands_has_the_readonly_process_runtime(
    tmp_path, tools, mode
):
    source = tmp_path / "source"
    task(
        source,
        '[environment]\nmemory_mb=128\ncpus=0.25\nstorage_mb=64\n[verifier]\nenvironment_mode="'
        + mode
        + '"\n',
    )
    images = metadata()
    if mode == "separate":
        images["native-task"]["verifier"] = dict(images["native-task"]["task"])
    output = tmp_path / "installed"
    install(
        source,
        output,
        revision="runtime-binding",
        worker_image=IMAGE,
        image_bindings=images,
        transfer_tools=tools,
    )
    assets = json.loads((output / "resources.json").read_text())["assets"]
    for asset in assets.values():
        if asset["kind"] == "container" and asset["role"] != "worker":
            runtime_mounts = [
                mount for mount in asset.get("mounts", []) if mount["target"] == "/evalhive-runtime"
            ]
            assert runtime_mounts == [
                {"file_id": "harness-runtime", "target": "/evalhive-runtime", "read_only": True}
            ]
