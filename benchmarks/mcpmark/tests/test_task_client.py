"""A Harness's workspace roots cannot replace the prepared native working copy."""

import json

import pytest
from evalhive_mcpmark.task_client import fixed_roots_frame, launch_config


@pytest.mark.parametrize(
    "service,field,environment",
    [
        ("github", "github_token", "GITHUB_PERSONAL_ACCESS_TOKEN"),
        ("notion", "notion_key", "OPENAPI_MCP_HEADERS"),
    ],
)
def test_external_native_client_keeps_credentials_out_of_command_arguments(
    service, field, environment
):
    argv, private = launch_config({"service": service, "config": {field: "fixture-private-token"}})
    assert "fixture-private-token" not in str(argv)
    assert "fixture-private-token" in private[environment]


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/admin",
        "http://owner-service:80/admin",
        "http://localhost:80@owner-service/admin",
    ],
)
def test_webarena_client_rejects_endpoints_outside_its_owned_namespace(url):
    with pytest.raises(ValueError, match="owned namespace"):
        launch_config(
            {
                "service": "playwright_webarena",
                "config": {"base_url": url, "environment": "webarena-docker", "docker": {}},
            }
        )


def test_native_filesystem_keeps_explicit_root_without_changing_other_mcp_capabilities():
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {"roots": {"listChanged": True}, "elicitation": {}},
            "clientInfo": {"name": "harness", "version": "1"},
        },
    }
    result = json.loads(fixed_roots_frame((json.dumps(request) + "\n").encode()))
    assert result["params"]["capabilities"] == {"elicitation": {}}
    assert result["params"]["clientInfo"] == request["params"]["clientInfo"]
    assert (
        fixed_roots_frame(b'{"jsonrpc":"2.0","method":"notifications/roots/list_changed"}\n')
        is None
    )
    tool_call = b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_allowed_directories","arguments":{}}}\n'
    assert fixed_roots_frame(tool_call) == tool_call
