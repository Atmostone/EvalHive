"""Public catalogs, retained worktrees and private evaluator storage stay separate."""

import asyncio
from copy import deepcopy

import pytest
from evalhive_benchmark import ProtocolError, validate, validate_environment
from evalhive_swebench.installation import declarations
from evalhive_swebench.native import REVISION, TASK_REVISION, VERSION
from evalhive_swebench.worker import MANIFEST, SWEbenchConnector

IMAGE = "sha256:" + "a" * 64


@pytest.fixture
def fixture():
    case = {
        "ref": {
            "dataset_id": "classic",
            "revision": "fixture",
            "case_id": "fixture__repo-1",
            "split": "test",
        },
        "title": "Public repository issue",
        "description": "Repair the reported bug",
        "source_digest": "sha256:" + "b" * 64,
        "native_ref": {"base_commit": "c" * 40},
        "requirements": {},
        "metadata": {"execution_availability": "installed", "installed_image": IMAGE},
    }
    receipt = {
        "format_version": "1.0",
        "connector_id": "swebench",
        "connector_version": "0.1.0",
        "dataset_id": "classic",
        "revision": "fixture",
        "source_digest": "sha256:" + "d" * 64,
        "worker_image": IMAGE,
        "cases": [case],
        "provenance": {
            "installed_images": {"fixture__repo-1": IMAGE},
            "native_revision": REVISION,
            "task_revision": TASK_REVISION,
            "native_version": VERSION,
        },
    }
    selected = {
        "image": IMAGE,
        "base_commit": "c" * 40,
        "image_commit": "c" * 40,
        "reserved_paths_checked": True,
        "task_limits": {"memory_mb": 1024, "cpu_cores": 1, "disk_mb": 512},
        "evaluator_limits": {"memory_mb": 768, "cpu_cores": 0.5, "disk_mb": 512},
    }
    assets, bindings = declarations(receipt, IMAGE, {"fixture__repo-1": selected})
    spec = {
        "protocol_version": "1.0",
        "scope": {"workspace_id": "workspace", "trial_id": "trial", "generation": 1},
        "case": case,
        "config": {},
        "models": {},
        "credential_refs": {},
        "limits": {"wall_time_seconds": 600, "memory_mb": 4096, "cpu_cores": 2, "disk_mb": 4096},
    }
    return receipt, bindings, assets, spec


def test_native_plan_keeps_private_tests_out_of_worker_and_agent_mounts(fixture):
    receipt, bindings, assets, spec = fixture
    worker = SWEbenchConnector(receipt=receipt, bindings=bindings)
    validate("manifest", MANIFEST)
    worker.load_spec(spec)
    plan = worker.plan()
    validate_environment(spec, plan)
    assert plan["agent"]["process_policy"] == "retain_until_cleanup"
    registered = assets["assets"]
    assert registered["swebench.worker"]["mounts"][0]["file_id"] == "catalog-data"
    task = registered[bindings["cases"]["fixture__repo-1"]["task"]]
    evaluator = registered[bindings["cases"]["fixture__repo-1"]["evaluator"]]
    assert set(task["storage_slots"]) == {"capture"}
    assert set(evaluator["storage_slots"]) == {"stage", "logs"}
    assert evaluator["storage_slots"]["stage"]["read_only"]
    assert not {"worker-data", "catalog-data"} & {item["file_id"] for item in task["mounts"]}
    world = registered["swebench.world"]
    assert world["storage_slots"]["logs"]["read_only"]
    assert world["storage_slots"]["capture"]["read_only"]
    assert world["outputs"]["report"]["visibility"] == "evaluator"


def test_uninstalled_image_stays_discoverable_but_cannot_launch(fixture):
    receipt, bindings, _, spec = fixture
    bindings["cases"]["fixture__repo-1"] = {
        "available": False,
        "reason": "NATIVE_IMAGE_NOT_INSTALLED",
    }
    receipt["cases"][0]["metadata"] = {
        "execution_availability": "unavailable",
        "unavailable_reason": "NATIVE_IMAGE_NOT_INSTALLED",
        "installed_image": None,
    }
    worker = SWEbenchConnector(receipt=receipt, bindings=bindings)
    result = asyncio.run(
        worker.handle("list_cases", {"dataset_id": "classic", "revision": "fixture"}, None)
    )
    assert (
        len(result["cases"]) == 1
        and result["cases"][0]["metadata"]["execution_availability"] == "unavailable"
    )
    with pytest.raises(ProtocolError) as rejected:
        worker.load_spec(spec)
    assert rejected.value.code == "NATIVE_IMAGE_NOT_INSTALLED" and not worker.handles


def test_unsupported_config_and_changed_case_fail_before_allocation(fixture):
    receipt, bindings, _, spec = fixture
    worker = SWEbenchConnector(receipt=receipt, bindings=bindings)
    changed = deepcopy(spec)
    changed["config"]["use_reference_patch"] = True
    with pytest.raises(ProtocolError):
        worker.load_spec(changed)
    changed = deepcopy(spec)
    changed["case"]["description"] = "Different issue"
    with pytest.raises(ProtocolError):
        worker.load_spec(changed)
    assert not worker.handles


def test_regrade_plan_needs_no_agent_and_rejects_another_source(fixture):
    receipt, bindings, _, spec = fixture
    spec["purpose"] = "evaluation"
    spec["checkpoint"] = {
        "format_version": "1.0",
        "connector": {"id": "swebench", "version": "0.1.0"},
        "case": spec["case"]["ref"],
        "source_digest": spec["case"]["source_digest"],
        "state_schema": "swebench.patch.v1",
        "complete": True,
        "evaluator_roles": [],
        "state": {
            "source_digest": spec["case"]["source_digest"],
            "base_commit": "c" * 40,
            "image_commit": "c" * 40,
            "patch_sha256": "e" * 64,
            "patch_size_bytes": 8,
        },
        "artifacts": [
            {
                "id": "patch",
                "uri": "evidence://patch",
                "digest": IMAGE,
                "size_bytes": 100,
                "visibility": "user",
            }
        ],
    }
    worker = SWEbenchConnector(receipt=receipt, bindings=bindings)
    worker.load_spec(spec)
    plan = worker.plan()
    validate_environment(spec, plan)
    assert plan["purpose"] == "evaluation" and "agent" not in plan
    assert "task" not in {resource["id"] for resource in plan["resources"]}
    assert next(resource for resource in plan["resources"] if resource["id"] == "world")["config"][
        "retained_inputs"
    ] == ["patch"]
    changed = deepcopy(spec)
    changed["checkpoint"]["source_digest"] = "sha256:" + "f" * 64
    with pytest.raises(ProtocolError):
        SWEbenchConnector(receipt=receipt, bindings=bindings).load_spec(changed)
