"""Expose native public policy and tool schemas without their effect handlers."""

from __future__ import annotations

from copy import deepcopy

from evalhive_benchmark.peer import identity

from .native import NativeConfigurationError, canonical, load_domain


def public_surface(root, domain):
    module = load_domain(root, domain)
    from tau2.agent.llm_agent import AGENT_INSTRUCTION, SYSTEM_PROMPT

    environment = module.get_environment()
    tools = []
    for tool in environment.get_tools():
        schema = tool.openai_schema
        if schema.get("type") != "function" or schema["function"]["name"] != tool.name:
            raise NativeConfigurationError(
                "Native tool cannot be represented by the public MCP surface"
            )
        definition = schema["function"]
        tools.append(
            {
                "name": tool.name,
                "description": definition.get("description", ""),
                "inputSchema": deepcopy(definition["parameters"]),
            }
        )
    if not 1 <= len(tools) <= 256 or len({tool["name"] for tool in tools}) != len(tools):
        raise NativeConfigurationError(
            "Native public tool catalog is empty, duplicated or oversized"
        )
    catalog = {"format_version": "1.0", "tools": tools}
    if len(canonical(catalog)) > 1024**2:
        raise NativeConfigurationError("Native public tool catalog exceeds its byte budget")
    policy = SYSTEM_PROMPT.format(
        agent_instruction=AGENT_INSTRUCTION, domain_policy=environment.get_policy()
    )
    return {
        "catalog": catalog,
        "instructions": policy,
        "schema_hashes": {tool["name"]: identity(tool["inputSchema"]) for tool in tools},
        "fingerprint": identity({"catalog": catalog, "instructions": policy}),
        "interface": "adapted_mcp_external_steps",
    }


def agent_resources(*, public_root="/evalhive-public", sdk_root="/opt/public-sdk"):
    return [
        {"id": "native-tools", "kind": "tools", "config": {"names": ["mcp__tau2__*"]}},
        {
            "id": "tau2",
            "kind": "mcp",
            "config": {
                "transport": "stdio",
                "command": "/opt/environment/venv/bin/python",
                "args": ["-m", "evalhive_benchmark.tool_catalog", public_root + "/tools.json"],
                "env": {"PYTHONPATH": sdk_root},
                "cwd": "/workspace",
            },
        },
    ]
