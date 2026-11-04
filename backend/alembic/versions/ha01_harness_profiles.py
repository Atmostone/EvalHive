"""Workspace-scoped harness profiles and write-only credential references.

Revision ID: ha01
Revises: fad4e5f6a7b8
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ha01"
down_revision = "fad4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "harness_profiles",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("connector_id", sa.String(200), nullable=False),
        sa.Column("connector_version", sa.String(100), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("secret_refs", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("model_id", sa.UUID(), sa.ForeignKey("llm_models.id", ondelete="SET NULL")),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "name", name="uq_harness_profiles_workspace_name"),
    )
    op.create_index("ix_harness_profiles_workspace_id", "harness_profiles", ["workspace_id"])
    op.create_index("uq_harness_profile_default", "harness_profiles", ["workspace_id"], unique=True,
                    postgresql_where=sa.text("is_default"))
    op.create_table(
        "harness_secrets",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "name", name="uq_harness_secrets_workspace_name"),
    )
    op.create_index("ix_harness_secrets_workspace_id", "harness_secrets", ["workspace_id"])
    # Populate declarations only; execution still follows the legacy path until H15.
    op.execute("""
        INSERT INTO harness_profiles (id, workspace_id, name, connector_id, connector_version, is_default)
        SELECT md5(id::text || '-builtin-pi')::uuid, id, 'Pi', 'pi', '0.1.0', true FROM workspaces
    """)


def downgrade():
    op.drop_table("harness_secrets")
    op.drop_table("harness_profiles")
