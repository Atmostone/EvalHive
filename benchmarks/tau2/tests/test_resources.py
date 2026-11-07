"""Public native schemas are identical to the private environment's discovery."""

import os

import pytest
from evalhive_benchmark.peer import identity
from evalhive_tau2.native import load_domain
from evalhive_tau2.resources import agent_resources, public_surface


@pytest.mark.parametrize("domain", ["airline", "retail", "telecom"])
def test_native_public_surface_contains_no_scenarios_or_effect_handlers(domain):
    root = os.environ.get("EVALHIVE_NATIVE_TAU2_DATA")
    if not root:
        pytest.skip("Requires the pinned native tau2 wheel and private baseline data")
    native = load_domain(root, domain).get_environment()
    surface = public_surface(root, domain)
    assert native.get_policy() in surface["instructions"]
    tools = {tool["name"]: tool for tool in surface["catalog"]["tools"]}
    for tool in native.get_tools():
        assert tools[tool.name]["inputSchema"] == tool.openai_schema["function"]["parameters"]
        assert surface["schema_hashes"][tool.name] == identity(tools[tool.name]["inputSchema"])
    assert "user_scenario" not in str(surface) and "evaluation_criteria" not in str(surface)
    assert surface == public_surface(root, domain)


def test_task_resources_contain_only_common_sdk_public_discovery():
    resources = agent_resources()
    assert resources[0]["config"]["names"] == ["mcp__tau2__*"]
    assert resources[1]["config"]["args"] == [
        "-m",
        "evalhive_benchmark.tool_catalog",
        "/evalhive-public/tools.json",
    ]
    assert "/dataset" not in str(resources) and "credential" not in str(resources)
