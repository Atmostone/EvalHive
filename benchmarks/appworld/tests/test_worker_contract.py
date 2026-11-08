"""The adapter declares owned state and exposes only the public MCP interface."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from evalhive_appworld.installation import declarations
from evalhive_appworld.worker import AppWorldConnector
from evalhive_benchmark import ProtocolError, validate, validate_environment

IMAGE = "sha256:" + "a" * 64
SCOPE = {"workspace_id": "workspace", "trial_id": "trial", "generation": 1}


@pytest.fixture
def connector(monkeypatch):
    from evalhive_appworld import worker

    monkeypatch.setattr(
        worker,
        "native_groups",
        lambda: [{"id": "appworld0", "apps": ["supervisor"], "tool_count": 6}],
    )
    case = {
        "ref": {
            "dataset_id": "appworld-dev",
            "revision": "fixture",
            "case_id": "abcdef0_1",
            "split": "dev",
        },
        "title": "Native fixture",
        "requirements": {},
        "source_digest": "sha256:" + "b" * 64,
        "native_ref": {"task_id": "abcdef0_1"},
    }
    receipt = {
        "source_digest": "sha256:" + "c" * 64,
        "cases": [case],
        "dataset_id": "appworld-dev",
        "revision": "fixture",
    }
    task = SimpleNamespace(
        instruction="Complete the task", supervisor={"email": "fixture@example.test"}
    )
    dataset = SimpleNamespace(
        receipt=receipt,
        resolve=lambda selected: (
            task if selected == case else (_ for _ in ()).throw(ValueError("changed case"))
        ),
    )
    resources, bindings = declarations(receipt, IMAGE, IMAGE)
    value = AppWorldConnector(dataset=dataset, bindings=bindings)
    spec = {
        "protocol_version": "1.0",
        "scope": SCOPE,
        "case": case,
        "config": {},
        "models": {},
        "credential_refs": {},
        "limits": {"wall_time_seconds": 600, "memory_mb": 4096, "cpu_cores": 2, "disk_mb": 2048},
    }
    return value, spec, resources


def test_plan_preserves_private_state_boundary_and_cleanup_order(connector):
    worker, spec, resources = connector
    validate("manifest", worker.manifest)
    asyncio.run(worker.handle("validate", {"spec": spec}, None))
    plan = asyncio.run(worker.handle("plan", {"spec": spec}, None))
    validate_environment(spec, plan)
    assert plan["grading_mode"] == "environment"
    assert (
        next(item for item in plan["resources"] if item["id"] == "task")["depends_on"][-1]
        == "world"
    )
    world, task = resources["assets"]["appworld.world"], resources["assets"]["appworld.task"]
    assert world["finalizer"]["replay_safe"]
    assert {
        "file_id": "harness-runtime",
        "target": "/evalhive-runtime",
        "read_only": True,
    } in world["mounts"]
    assert "state" not in task["storage_slots"]
    assert task["command"] == ["sleep", "infinity"]
    assert not {"native-data", "worker-data"} & {mount["file_id"] for mount in task["mounts"]}
    assert all(
        output["visibility"] == "evaluator"
        for output in plan["outputs"]
        if output["resource_id"] == "world"
    )
    assert plan["agent"]["resources"][1]["config"]["args"] == [
        "/opt/environment/appworld_mcp.py",
        "appworld0",
    ]
    assert plan["agent"]["environment_handle"] == "task"


def test_changed_case_or_runtime_bindings_fail_before_resources(connector):
    worker, spec, resources = connector
    changed = deepcopy(spec)
    changed["case"]["native_ref"]["task_id"] = "aaaaaaa_1"
    with pytest.raises(ProtocolError, match="dataset or runtime"):
        asyncio.run(worker.handle("validate", {"spec": changed}, None))
    worker.bindings["source_digest"] = "sha256:" + "d" * 64
    with pytest.raises(ProtocolError, match="dataset or runtime"):
        asyncio.run(worker.handle("validate", {"spec": spec}, None))
