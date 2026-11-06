"""Compile workspace resources into public declarations and private credentials."""

from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path

from evalhive_harness import resources

from app.auth.tokens import issue_runner_token
from app.config import get_settings

KNOWLEDGE_REF = "evalhive_knowledge_token"


def knowledge_resource(attempt_id):
    base = get_settings().runner_api_url.rstrip("/")
    return {
        "id": "evalhive_knowledge",
        "kind": "mcp",
        "config": {
            "transport": "http",
            "url": f"{base}/api/v1/attempts/{attempt_id}/knowledge/mcp",
            "aliases": {"search_knowledge_base": "search_knowledge_base"},
        },
        "credential_refs": {"headers.Authorization": KNOWLEDGE_REF},
    }


def assemble_resources(
    *,
    tools=None,
    mcp_servers=(),
    memory_mode="off",
    memory_context="",
    rules="",
    skill_paths=(),
    attempt_id=None,
):
    """None selects connector defaults; [] explicitly disables builtin tools."""
    if memory_mode not in {"off", "flat", "structured"}:
        raise ValueError("Unsupported memory mode")
    declarations, credentials = [], {}
    selected = None if tools is None else list(tools)
    if selected is not None and "search_knowledge_base" in selected:
        if attempt_id is None:
            raise ValueError("Knowledge access requires an attempt identity")
        declarations.append(knowledge_resource(attempt_id))
    for index, server in enumerate(mcp_servers):
        identity = re.sub(r"[^A-Za-z0-9_-]", "_", server.get("name") or f"server_{index}")
        identity = f"m{index}_{identity}"[:32]
        if server.get("url") and server.get("command"):
            raise ValueError("Select one MCP transport")
        http = bool(server.get("url"))
        config = {
            key: deepcopy(server[key])
            for key in (("url",) if http else ("command", "args", "cwd"))
            if server.get(key) is not None
        }
        config["transport"] = "http" if http else "stdio"
        config["alias_prefix"] = (server.get("name") or f"server_{index}") + "__"
        refs = {}
        for section in ("headers",) if http else ("env",):
            # Registry HTTP secrets name headers; stdio secrets name env vars.
            values = server.get(section, server.get("env", {}) if http else {})
            for name, value in values.items():
                reference = f"mcp_{index}_{section}_{name}"
                credentials[reference] = str(value)
                refs[f"{section}.{name}"] = reference
        declarations.append(
            {
                "id": identity,
                "kind": "mcp",
                "config": config,
                **({"credential_refs": refs} if refs else {}),
            }
        )
        if selected is not None:
            selected.append(f"mcp__{identity}__*")
    if selected is not None:
        declarations.insert(0, {"id": "tools", "kind": "tools", "config": {"names": selected}})
    if rules:
        declarations.append({"id": "rules", "kind": "instructions", "config": {"content": rules}})
    declarations.append(
        {
            "id": "memory",
            "kind": "memory",
            "config": {
                "mode": memory_mode,
                **({"content": memory_context} if memory_mode != "off" else {}),
            },
        }
    )
    declarations.extend(
        {"id": f"skill_{i}", "kind": "skill", "config": {"path": path}}
        for i, path in enumerate(skill_paths)
    )
    resources({"environment": {"resources": declarations}})
    return declarations, credentials


def workspace_context(workspace_id, memory_mode):
    root = Path(get_settings().data_dir) / "shared" / str(workspace_id)

    def read(name):
        path = root / name
        if root.is_symlink() or path.is_symlink():
            raise ValueError("Workspace context cannot be a symlink")
        if not path.exists():
            return ""
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("Workspace context exceeds the size limit")
        return path.read_text()

    return read("rules.md"), read("memory.md") if memory_mode == "flat" else ""


async def provision_knowledge(db, attempt, spec, credentials):
    declarations = resources(spec)
    expected = knowledge_resource(attempt.id)
    if expected not in declarations:
        if any(KNOWLEDGE_REF in item.get("credential_refs", {}).values() for item in declarations):
            raise ValueError("Knowledge resource does not match its attempt")
        return credentials
    token = await issue_runner_token(
        db,
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        workspace_id=attempt.workspace_id,
        kind="knowledge",
    )
    return {**credentials, KNOWLEDGE_REF: "Bearer " + token}
