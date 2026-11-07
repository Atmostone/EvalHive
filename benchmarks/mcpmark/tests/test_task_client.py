"""A Harness's workspace roots cannot replace the prepared native working copy."""

import json

from evalhive_mcpmark.task_client import fixed_roots_frame


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
