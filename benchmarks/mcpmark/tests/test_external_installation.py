"""Every external variant installs and plans through the same SDK worker."""

import asyncio
import json
import os
from copy import deepcopy

import pytest
from evalhive_benchmark import validate_environment
from evalhive_mcpmark.dataset import InstalledMCPMark, build_receipt
from evalhive_mcpmark.external_state import github_template
from evalhive_mcpmark.installation import install
from evalhive_mcpmark.worker import MCPMarkConnector
from test_external_resources import IMAGE, SETTINGS


@pytest.mark.parametrize("service", SETTINGS)
def test_external_installation_freezes_settings_and_plans_without_accounts_or_models(
    service, tmp_path
):
    source = os.getenv("EVALHIVE_MCPMARK_NATIVE_ROOT")
    if not source:
        pytest.skip("Requires the pinned native catalog and manager dependencies")
    initial = tmp_path / "initial"
    initial.mkdir()
    settings = deepcopy(SETTINGS[service])
    options = {"service_settings": settings, "world_image": IMAGE}
    if service == "github":
        template = "codecrafters-io-build-your-own-x"
        (initial / template).mkdir()
        (initial / template / "fixture.txt").write_text("Frozen public template fixture")
        options["categories"] = [template]
    if service == "playwright_webarena":
        options.update(
            site_image=IMAGE, site_limits={"memory_mb": 4096, "cpu_cores": 2, "disk_mb": 16384}
        )
    output = tmp_path / "installed"
    receipt = install(
        source,
        output,
        initial_root=initial,
        revision="verified",
        worker_image=IMAGE,
        task_image=IMAGE,
        service=service,
        **options,
    )
    dataset = InstalledMCPMark(output / "worker-data/native", receipt)
    selected = next(
        case
        for case in receipt["cases"]
        if service != "github" or github_template(case["native_ref"]["category_id"]) == template
    )
    bindings = json.loads((output / "worker-data/runtime.json").read_text())
    worker = MCPMarkConnector(dataset=dataset, bindings=bindings)
    spec = {
        "protocol_version": "1.0",
        "scope": {"workspace_id": "workspace", "trial_id": "trial", "generation": 1},
        "case": selected,
        "config": {},
        "models": {},
        "credential_refs": {"service_credentials": "fixture-private-reference"}
        if service in {"github", "notion"}
        else {},
        "limits": {"wall_time_seconds": 600, "memory_mb": 8192, "cpu_cores": 4, "disk_mb": 32768},
    }
    asyncio.run(worker.handle("validate", {"spec": spec}, None))
    plan = asyncio.run(worker.handle("plan", {"spec": spec}, None))
    validate_environment(spec, plan)
    assert plan["agent"]["prompt"]
    assert all("source_key" not in resource["config"] for resource in plan["agent"]["resources"])
    changed = deepcopy(settings)
    if service == "github":
        changed["evaluation_owner"] = "different-dedicated-fixture"
    elif service == "notion":
        changed["source_hub"] = "Another frozen source hub"
    elif service == "playwright":
        changed["browser"] = "firefox"
    else:
        changed["readiness_timeout_seconds"] = 601
    updated = build_receipt(
        source,
        "verified",
        IMAGE,
        service=service,
        assets=receipt["provenance"].get("initial_assets"),
        service_settings=changed,
    )
    assert updated["source_digest"] != receipt["source_digest"]
