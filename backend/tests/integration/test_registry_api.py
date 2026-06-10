"""Integration tests for the Tool & MCP Registry endpoints (SPA-41).

CRUD + masking, the owner/admin gate, the connection test, the delete guard
(409 when referenced, force to strip refs), template `tool_ids` validation, and the
end-to-end spawn resolver (registry refs + run_config override → materialized
(tools, mcp_servers)).
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import text

from app import database
from app.models.template import Template
from app.registry.resolver import resolve_template_tools


async def _create(client: AsyncClient, **body):
    return await client.post("/api/registry/tools", json=body)


async def _make_member(ws):
    async with database.async_session() as s:
        await s.execute(
            text("UPDATE workspace_members SET role='member' WHERE workspace_id=:w"),
            {"w": str(ws)},
        )
        await s.commit()


# --------------------------------------------------------------------------- #
# CRUD + masking
# --------------------------------------------------------------------------- #
async def test_create_list_mask_update(auth_client: AsyncClient):
    r = await _create(
        auth_client,
        name="github",
        kind="mcp",
        config={"command": "npx", "args": ["-y", "server-github"]},
        secrets={"GITHUB_TOKEN": "ghp_secret9999"},
    )
    assert r.status_code == 201, r.text
    entry = r.json()
    assert entry["kind"] == "mcp"
    assert entry["secrets"]["GITHUB_TOKEN"] == "***9999"  # masked, not raw
    assert entry["secret_keys"] == ["GITHUB_TOKEN"]

    r = await auth_client.get("/api/registry/tools")
    assert r.status_code == 200
    listed = r.json()
    assert any(e["name"] == "github" and e["secrets"]["GITHUB_TOKEN"] == "***9999" for e in listed)

    r = await auth_client.put(f"/api/registry/tools/{entry['id']}", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False


async def test_create_mcp_requires_command_or_url(auth_client: AsyncClient):
    r = await _create(auth_client, name="bad", kind="mcp", config={})
    assert r.status_code == 400


async def test_duplicate_name_conflicts(auth_client: AsyncClient):
    assert (await _create(auth_client, name="uniqdup", kind="builtin")).status_code == 201
    r = await _create(auth_client, name="uniqdup", kind="builtin")
    assert r.status_code == 400


async def test_create_requires_admin(auth_client: AsyncClient):
    ws = auth_client.headers["X-Workspace-Id"]
    await _make_member(ws)
    r = await _create(auth_client, name="bash", kind="builtin")
    assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Connection test
# --------------------------------------------------------------------------- #
async def test_test_endpoint_variants(auth_client: AsyncClient):
    bi = (await _create(auth_client, name="mytool", kind="builtin")).json()
    r = await auth_client.post(f"/api/registry/tools/{bi['id']}/test")
    assert r.status_code == 200 and r.json()["ok"] is True

    stdio = (
        await _create(auth_client, name="gh", kind="mcp", config={"command": "npx"})
    ).json()
    r = await auth_client.post(f"/api/registry/tools/{stdio['id']}/test")
    assert r.json()["ok"] is True and "sandbox" in r.json()["detail"]

    http = (
        await _create(
            auth_client, name="remote", kind="mcp", config={"url": "http://127.0.0.1:9/x"}
        )
    ).json()
    r = await auth_client.post(f"/api/registry/tools/{http['id']}/test")
    assert r.json()["ok"] is False  # nothing listening


# --------------------------------------------------------------------------- #
# Delete guard
# --------------------------------------------------------------------------- #
async def test_delete_guard_and_force(auth_client: AsyncClient):
    entry = (await _create(auth_client, name="mytool", kind="builtin")).json()
    tmpl = (
        await auth_client.post(
            "/api/templates",
            json={
                "name": "Coder",
                "description": "d",
                "soul_md": "s",
                "tool_ids": [entry["id"]],
            },
        )
    ).json()

    r = await auth_client.delete(f"/api/registry/tools/{entry['id']}")
    assert r.status_code == 409

    r = await auth_client.delete(f"/api/registry/tools/{entry['id']}?force=true")
    assert r.status_code == 204

    r = await auth_client.get(f"/api/templates/{tmpl['id']}")
    assert r.json()["tool_ids"] == []  # reference stripped


# --------------------------------------------------------------------------- #
# Template validation + end-to-end resolution
# --------------------------------------------------------------------------- #
async def test_template_rejects_unknown_tool_id(auth_client: AsyncClient):
    r = await auth_client.post(
        "/api/templates",
        json={"name": "X", "description": "d", "soul_md": "s", "tool_ids": [str(uuid.uuid4())]},
    )
    assert r.status_code == 400


async def test_resolver_materializes_and_applies_override(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    bash = (await _create(auth_client, name="mytool", kind="builtin")).json()
    gh = (
        await _create(
            auth_client,
            name="github",
            kind="mcp",
            config={"command": "npx", "args": ["-y"]},
            secrets={"GITHUB_TOKEN": "ghp_x"},
        )
    ).json()
    tmpl = (
        await auth_client.post(
            "/api/templates",
            json={
                "name": "Coder",
                "description": "d",
                "soul_md": "s",
                "tool_ids": [bash["id"], gh["id"]],
            },
        )
    ).json()

    async with database.async_session() as db:
        template = await db.get(Template, uuid.UUID(tmpl["id"]))
        tools, mcp = await resolve_template_tools(db, template)
        assert tools == ["mytool"]
        assert mcp == [
            {"name": "github", "command": "npx", "args": ["-y"], "env": {"GITHUB_TOKEN": "ghp_x"}}
        ]

        # A task-level override disables bash and the resolver drops it.
        tools2, mcp2 = await resolve_template_tools(
            db, template, run_config={"tools_override": {"disable": [bash["id"]]}}
        )
        assert tools2 == []
        assert len(mcp2) == 1

    assert ws  # workspace header present (sanity)
