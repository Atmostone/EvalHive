"""tool & mcp registry — user-level Tool & MCP Registry (SPA-41)

Adds the registry_entries table (a workspace-level source of truth for tools and MCP
servers) and migrates every template's inline tools/mcp_servers into it (big-bang):
distinct builtin names + deduped MCP configs become registry rows, and each template
is rewritten to reference them by id via the new templates.tool_ids. The inline
templates.tools / templates.mcp_servers columns are then dropped. template_versions
gains a nullable tool_ids for new snapshots (old snapshot rows keep their inline copy).

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-06-10
"""

import json
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.registry.service import dedupe_for_migration


revision = "a5b6c7d8e9f0"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "registry_entries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False, server_default="builtin"),
        sa.Column("config", JSONB(), nullable=False, server_default="{}"),
        sa.Column("secrets", JSONB(), nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), server_default="user"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("idx_registry_entries_workspace", "registry_entries", ["workspace_id"])
    op.create_unique_constraint(
        "uq_registry_entries_workspace_name", "registry_entries", ["workspace_id", "name"]
    )

    op.add_column(
        "templates",
        sa.Column("tool_ids", JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column("template_versions", sa.Column("tool_ids", JSONB(), nullable=True))

    # --- data migration: inline tools/mcp_servers → registry + tool_ids refs ---
    bind = op.get_bind()
    workspaces = bind.execute(sa.text("SELECT DISTINCT workspace_id FROM templates")).fetchall()
    for (ws_id,) in workspaces:
        rows = bind.execute(
            sa.text("SELECT id, tools, mcp_servers FROM templates WHERE workspace_id = :w"),
            {"w": ws_id},
        ).fetchall()
        templates = [
            {"id": str(r[0]), "tools": r[1] or [], "mcp_servers": r[2] or []} for r in rows
        ]
        entries, per_template = dedupe_for_migration(templates)

        key_to_id: dict = {}
        for e in entries:
            new_id = uuid.uuid4()
            key_to_id[e["key"]] = str(new_id)
            bind.execute(
                sa.text(
                    "INSERT INTO registry_entries "
                    "(id, workspace_id, name, kind, config, secrets, enabled, created_by) "
                    "VALUES (:id, :w, :name, :kind, CAST(:config AS JSONB), "
                    "CAST(:secrets AS JSONB), true, 'migration')"
                ),
                {
                    "id": str(new_id),
                    "w": ws_id,
                    "name": e["name"],
                    "kind": e["kind"],
                    "config": json.dumps(e["config"]),
                    "secrets": json.dumps(e["secrets"]),
                },
            )
        for tid, keys in per_template.items():
            ids = [key_to_id[k] for k in keys]
            bind.execute(
                sa.text("UPDATE templates SET tool_ids = CAST(:t AS JSONB) WHERE id = :id"),
                {"t": json.dumps(ids), "id": tid},
            )

    op.drop_column("templates", "tools")
    op.drop_column("templates", "mcp_servers")


def downgrade():
    op.add_column(
        "templates", sa.Column("tools", JSONB(), nullable=False, server_default="[]")
    )
    op.add_column(
        "templates", sa.Column("mcp_servers", JSONB(), nullable=False, server_default="[]")
    )

    bind = op.get_bind()
    entries = bind.execute(
        sa.text("SELECT id, name, kind, config, secrets FROM registry_entries")
    ).fetchall()
    by_id = {str(r[0]): r for r in entries}

    rows = bind.execute(sa.text("SELECT id, tool_ids FROM templates")).fetchall()
    for tid, tool_ids in rows:
        tool_ids = tool_ids or []
        if not tool_ids:
            continue
        tools: list = []
        mcp: list = []
        for i in tool_ids:
            r = by_id.get(str(i))
            if r is None:
                continue
            _, name, kind, config, secrets = r
            if kind == "builtin":
                tools.append(name)
            else:
                config = config or {}
                mcp.append(
                    {
                        "name": name,
                        "command": config.get("command"),
                        "args": config.get("args") or [],
                        "env": secrets or {},
                    }
                )
        bind.execute(
            sa.text(
                "UPDATE templates SET tools = CAST(:t AS JSONB), "
                "mcp_servers = CAST(:m AS JSONB) WHERE id = :id"
            ),
            {"t": json.dumps(tools), "m": json.dumps(mcp), "id": str(tid)},
        )

    op.drop_column("templates", "tool_ids")
    op.drop_column("template_versions", "tool_ids")
    op.drop_table("registry_entries")
