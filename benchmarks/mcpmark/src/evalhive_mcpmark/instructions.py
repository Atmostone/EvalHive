"""Read the pinned public baseline instruction without importing its LLM runner."""

import ast

from .native import NativeConfigurationError, native_code


def native_instruction():
    tree = ast.parse((native_code() / "src/agents/mcpmark_agent.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "MCPMarkAgent":
            for field in node.body:
                if isinstance(field, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "SYSTEM_PROMPT"
                    for target in field.targets
                ):
                    value = ast.literal_eval(field.value)
                    if isinstance(value, str) and value:
                        return value
    raise NativeConfigurationError("Pinned native baseline instruction is unavailable")
