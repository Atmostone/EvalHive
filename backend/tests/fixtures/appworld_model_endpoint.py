"""Offline model responses exercise native task lookup and a durable MCP effect."""

import json

from tests.fixtures.mcpmark_postgres_endpoint import MCPMarkPostgresEndpoint


class AppWorldModelEndpoint(MCPMarkPostgresEndpoint):
    model = "fixture-appworld"

    def message(self, body, step):
        names = [tool["function"]["name"] for tool in body["tools"]]
        assert len(names) == 469
        assert not any("admin__" in name or "api_docs__" in name for name in names)
        if step >= 2:
            return {"role": "assistant", "content": "Engineering fixture completed"}
        operation = "show_active_task" if step == 0 else "complete_task"
        name = next(name for name in names if name.endswith("supervisor__" + operation))
        arguments = {} if step == 0 else {"status": "success", "answer": "Engineering fixture"}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "appworld-native-" + str(step),
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }
