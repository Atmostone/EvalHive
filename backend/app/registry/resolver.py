"""Spawn-time tool/MCP resolution (SPA-41).

Materializes a template's registry references (``templates.tool_ids``) — plus any
task-level ``run_config.tools_override`` — into the exact shapes the agent container
consumes: a list of builtin tool names (``AGENT_TOOLS``) and a list of MCP server
dicts ``{name, command, args, env}`` (``MCP_SERVERS``). This is the single place that
reveals secrets into the container env; every API read masks them.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry_entry import RegistryEntry

logger = logging.getLogger(__name__)


def _apply_override(tool_ids: list[str], override: Optional[dict]) -> list[str]:
    """Apply ``{enable:[ids], disable:[ids]}`` to the template's id list.

    Finest-restriction-wins: a `disable` removes an id even if `enable` lists it;
    `enable` appends ids not already present. Order is preserved (template order,
    then newly-enabled ids in their given order)."""
    if not isinstance(override, dict):
        return list(tool_ids)
    disable = {str(x) for x in (override.get("disable") or [])}
    enable = [str(x) for x in (override.get("enable") or [])]
    result = [i for i in tool_ids if i not in disable]
    seen = set(result)
    for i in enable:
        if i in disable or i in seen:
            continue
        result.append(i)
        seen.add(i)
    return result


def _materialize(entry: RegistryEntry) -> Optional[object]:
    """A builtin entry → its tool name (str); an mcp entry → a server dict."""
    if entry.kind == "builtin":
        return entry.name
    config = entry.config or {}
    server = {
        "name": entry.name,
        "command": config.get("command"),
        "args": list(config.get("args") or []),
        "env": dict(entry.secrets or {}),
    }
    if config.get("url"):
        server["url"] = config["url"]
    return server


async def resolve_template_tools(
    db: AsyncSession, template, *, run_config: Optional[dict] = None
) -> tuple[list, list]:
    """Resolve ``(tools, mcp_servers)`` for a template + optional task override.

    Skips disabled and missing registry entries (logged). Returns the builtin tool
    name list and the MCP server dicts, in the effective reference order."""
    tool_ids = [str(i) for i in (getattr(template, "tool_ids", None) or [])]
    override = (run_config or {}).get("tools_override") if isinstance(run_config, dict) else None
    effective = _apply_override(tool_ids, override)
    if not effective:
        return [], []

    uuids = []
    for i in effective:
        try:
            uuids.append(uuid.UUID(i))
        except (ValueError, TypeError):
            logger.warning("registry: bad tool id %r on template %s", i, getattr(template, "id", "?"))
    rows = (
        await db.execute(select(RegistryEntry).where(RegistryEntry.id.in_(uuids)))
    ).scalars().all() if uuids else []
    by_id = {str(r.id): r for r in rows}

    ws_id = getattr(template, "workspace_id", None)
    tools: list = []
    mcp_servers: list = []
    for i in effective:
        entry = by_id.get(i)
        if entry is None:
            logger.warning("registry: tool id %s referenced by template %s not found", i, getattr(template, "id", "?"))
            continue
        if ws_id is not None and entry.workspace_id != ws_id:
            logger.warning("registry: tool id %s is cross-workspace; skipped", i)
            continue
        if not entry.enabled:
            continue
        materialized = _materialize(entry)
        if entry.kind == "builtin":
            tools.append(materialized)
        else:
            mcp_servers.append(materialized)
    return tools, mcp_servers
