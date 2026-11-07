"""Native installation snapshots preserve source bytes and explicit image budgets."""

import hashlib
import json

import pytest
from evalhive_harbor.dataset import InstalledHarbor
from evalhive_harbor.installation import install
from evalhive_harbor.native import HarborConfigurationError
from evalhive_harbor.worker import HarborConnector
from test_native import task

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
