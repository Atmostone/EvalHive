"""Partition complete native app catalogs within each harness MCP server budget."""

from __future__ import annotations

from .native import NativeConfigurationError, canonical

MAX_TOOLS, MAX_BYTES = 256, 768 * 1024


def partition(catalogs):
    if not catalogs or len(catalogs) > 32 or {"admin", "api_docs"} & set(catalogs):
        raise NativeConfigurationError("Native MCP groups require public app catalogs")
    groups, identities = [], set()
    ordered = sorted(catalogs.items(), key=lambda pair: (-len(pair[1]), pair[0]))
    for app, tools in ordered:
        size = len(canonical(tools))
        if not tools or len(tools) > MAX_TOOLS or size > MAX_BYTES:
            raise NativeConfigurationError("Native app catalog exceeds a supported MCP budget")
        for tool in tools:
            identity = tool["name"]
            if identity in identities or not identity.startswith(app + "__"):
                raise NativeConfigurationError(
                    "Native public MCP tools have inconsistent identities"
                )
            identities.add(identity)
        candidates = [
            group
            for group in groups
            if group["tools"] + len(tools) <= MAX_TOOLS and group["bytes"] + size <= MAX_BYTES
        ]
        if candidates:
            selected = min(candidates, key=lambda group: (group["tools"], group["id"]))
        else:
            selected = {"id": "appworld" + str(len(groups)), "apps": [], "tools": 0, "bytes": 0}
            groups.append(selected)
        selected["apps"].append(app)
        selected["tools"] += len(tools)
        selected["bytes"] += size
    return [
        {"id": group["id"], "apps": sorted(group["apps"]), "tool_count": group["tools"]}
        for group in groups
    ]


def native_groups():
    from appworld.collections.api_docs import ApiDocCollection
    from appworld.serve._mcp import DEFAULT_APP_NAMES

    return partition(
        {app: ApiDocCollection.build(load_apps=(app,)).mcp() for app in DEFAULT_APP_NAMES}
    )
