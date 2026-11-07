"""External resource plans preserve isolation and do not lower site requirements."""

import pytest
from evalhive_benchmark import validate_environment
from evalhive_mcpmark.external_resources import configure_assets, environment_plan
from evalhive_mcpmark.installation import declarations
from evalhive_mcpmark.native import NativeConfigurationError

IMAGE = "sha256:" + "a" * 64
RECEIPT = {"source_digest": "sha256:" + "b" * 64, "provenance": {"service": "filesystem"}}
SETTINGS = {
    "github": {"evaluation_owner": "dedicated-fixture", "dedicated_target": True},
    "notion": {
        "source_hub": "Fixture Source",
        "evaluation_hub": "Fixture Evaluation",
        "dedicated_target": True,
    },
    "playwright": {},
    "playwright_webarena": {
        "site": {
            "image_alias": "shopping_admin_final_0719",
            "container_alias": "shopping_admin",
            "port": 80,
        }
    },
}


@pytest.mark.parametrize("service", SETTINGS)
def test_external_plans_use_owned_networks_and_keep_private_inputs_out_of_task(service):
    resources, bindings = declarations(RECEIPT, IMAGE, IMAGE)
    assets = resources["assets"]
    site = service == "playwright_webarena"
    names = configure_assets(
        assets,
        service,
        SETTINGS[service],
        world_image=IMAGE,
        **(
            {
                "site_image": IMAGE,
                "site_limits": {"memory_mb": 4096, "cpu_cores": 2, "disk_mb": 16384},
            }
            if site
            else {}
        ),
    )
    bindings["assets"] = {name: "mcpmark." + name for name in names}
    agent = {
        "mode": "in_environment",
        "interaction": "autonomous",
        "environment_handle": "task",
        "cwd": "/workspace",
        "prompt": "Native fixture",
        "instructions": [],
        "resources": [],
    }
    plan = environment_plan(bindings, service, agent)
    spec = {
        "protocol_version": "1.0",
        "scope": {"workspace_id": "workspace", "trial_id": "trial", "generation": 1},
        "case": {
            "ref": {"dataset_id": "native-fixture", "revision": "verified", "case_id": "fixture"},
            "title": "Native fixture",
            "source_digest": RECEIPT["source_digest"],
            "native_ref": {},
            "requirements": {},
        },
        "config": {},
        "models": {},
        "credential_refs": {},
        "limits": {"wall_time_seconds": 600, "memory_mb": 8192, "cpu_cores": 4, "disk_mb": 32768},
    }
    validate_environment(spec, plan)
    assert assets["mcpmark.network"]["egress"] is (not site)
    assert assets["mcpmark.task"]["storage_slots"]["connection"]["read_only"] is True
    assert all(mount["file_id"] == "harness-runtime" for mount in assets["mcpmark.task"]["mounts"])
    assert assets["mcpmark.task"]["storage_slots"]["working"]["target"] == "/workspace"
    selected = {resource["id"]: resource for resource in plan["resources"]}
    if site:
        assert (
            selected["world"]["config"]["namespace"]
            == selected["site"]["config"]["namespace"]
            == "task"
        )
        assert assets["mcpmark.site"]["limits"]["memory_mb"] == 4096
        assert assets["mcpmark.site"]["minimum"]["disk_mb"] == 16384
        assert "ports" not in assets["mcpmark.site"]
        assert "network" not in selected["site"]["config"]


def test_external_world_requires_an_explicit_immutable_image():
    resources, _ = declarations(RECEIPT, IMAGE, IMAGE)
    with pytest.raises(NativeConfigurationError, match="immutable"):
        configure_assets(resources["assets"], "playwright", {}, world_image=None)
