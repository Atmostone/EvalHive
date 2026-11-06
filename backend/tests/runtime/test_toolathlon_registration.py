"""Native package data compiles through the unchanged generic provider contract."""

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from app.runtime.admission import GroupPolicy, HostCapacity, check_group
from app.runtime.managed_spec import OwnerScope, PathMapping, RuntimePolicy
from app.runtime.registered_assets import RegisteredFile
from app.runtime.registered_files import load_registered_assets


@pytest.mark.parametrize("purpose", ["execution", "evaluation"])
def test_native_toolathlon_assets_preserve_budget_gold_boundary_and_evaluation_only_plan(
    purpose, tmp_path
):
    installation = pytest.importorskip("evalhive_toolathlon.installation")
    worker_module = pytest.importorskip("evalhive_toolathlon.worker")

    async def scenario():
        worker = worker_module.ToolathlonConnector()
        worker.spec = {"purpose": purpose, "checkpoint": {"artifacts": [{"id": "checkpoint"}]}}
        worker.case = SimpleNamespace(agent_input=lambda: {"prompt": "Public task"})
        worker.resources = []
        worker.instructions = []
        plan = worker._plan()
        image = "sha256:" + "a" * 64
        origin = OwnerScope(*(str(uuid.uuid4()) for _ in range(3)), 1)
        package = tmp_path / "installed"
        package.mkdir()
        for name in ("cases", "native-source", "harness-runtime"):
            (package / name).mkdir()
        mapping = PathMapping("/installed", str(package))
        runtime = SimpleNamespace(
            policy=RuntimePolicy(PathMapping("/owned", "/owned"), (mapping,)),
            resolve_image=lambda reference: {"image_id": reference},
        )
        (package / "resources.json").write_text(
            json.dumps(installation.assets(image, image, image, egress=False))
        )
        (package / "files.json").write_text(
            json.dumps(
                {
                    "format_version": "1.0",
                    "files": {
                        "cases": {
                            "kind": "relative",
                            "path": "cases",
                            "source_digest": "sha256:" + "b" * 64,
                        },
                        "native-source": {
                            "kind": "relative",
                            "path": "native-source",
                            "source_digest": "sha256:" + "b" * 64,
                        },
                        "harness-runtime": {"kind": "runtime_binding", "name": "harness.runtime"},
                    },
                }
            )
        )
        registered = load_registered_assets(
            mapping,
            runtime,
            worker_module.MANIFEST,
            bindings={
                "harness.runtime": RegisteredFile(
                    "/installed/harness-runtime", "sha256:" + "b" * 64
                )
            },
        )
        retained = (
            {"checkpoint": RegisteredFile("/owned/retained/checkpoint", "sha256:" + "c" * 64)}
            if purpose == "evaluation"
            else {}
        )
        compiled = await registered.compile(origin, plan, retained_inputs=retained)
        _, worker_demand, _ = await registered.resource(
            origin, "worker", "toolathlon-gym.worker", kind="container"
        )
        decision = check_group(
            (worker_demand, *compiled.demands),
            worker_module.MANIFEST["requirements"]["resources"],
            HostCapacity(8192, 20000, 2, "fixture"),
            runtime.policy,
            GroupPolicy(),
            active_trials=0,
        )
        assert decision["eligible"], decision
        assert decision["group_resources"] == {
            "memory_mb": 2112 if purpose == "execution" else 1088,
            "cpu_cores": pytest.approx(1.4 if purpose == "execution" else 0.4),
            "disk_mb": 1536 if purpose == "execution" else 1408,
        }
        assert compiled.leases["world"][1].intent.role == "service"
        assert any(
            mount.target == "/evalhive-runtime" and mount.read_only
            for mount in compiled.leases["world"][1].intent.mounts
        )
        if purpose == "execution":
            task = compiled.leases["task"][1]
            assert [mount.target for mount in task.intent.mounts] == ["/evalhive-runtime"]
            assert compiled.links["task"]["namespace"] == "world"
        else:
            assert "task" not in compiled.leases and "agent" not in plan
        assert [
            mount.target
            for mount in compiled.leases["world"][1].intent.mounts
            if mount.source == "/installed/native-source"
        ] == ["/native/native-source.tar"]

    asyncio.run(scenario())
