"""Workspace benchmark profiles and immutable dataset metadata.

Revision ID: bg01
Revises: ha09
Create Date: 2026-11-05
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "bg01"
down_revision = "ha09"
branch_labels = None
depends_on = None


def _identity():
    return [sa.Column("id", sa.UUID(), primary_key=True),
            sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)]


def _created():
    return sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now())


def upgrade():
    op.create_table("benchmark_profiles", *_identity(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("connector_id", sa.String(200), nullable=False),
        sa.Column("connector_version", sa.String(200), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("secret_refs", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("model_bindings", postgresql.JSONB(), nullable=False, server_default="{}"),
        _created(), sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "name", name="uq_benchmark_profile_name"))
    op.create_index("ix_benchmark_profiles_workspace_id", "benchmark_profiles", ["workspace_id"])
    op.create_table("benchmark_secrets", *_identity(),
        sa.Column("name", sa.String(200), nullable=False), sa.Column("value", sa.Text(), nullable=False),
        _created(), sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "name", name="uq_benchmark_secret_name"))
    op.create_index("ix_benchmark_secrets_workspace_id", "benchmark_secrets", ["workspace_id"])
    op.create_table("benchmark_dataset_revisions", *_identity(),
        sa.Column("connector_id", sa.String(200), nullable=False),
        sa.Column("connector_version", sa.String(200), nullable=False),
        sa.Column("dataset_id", sa.String(200), nullable=False), sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("dataset_key", sa.String(64), nullable=False),
        sa.Column("source_digest", sa.String(71), nullable=False),
        sa.Column("receipt_digest", sa.String(71), nullable=False), sa.Column("worker_image", sa.Text(), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("case_count", sa.Integer(), nullable=False), _created(),
        sa.UniqueConstraint("workspace_id", "connector_id", "connector_version", "dataset_key", name="uq_benchmark_dataset_revision"),
        sa.CheckConstraint("case_count >= 0", name="ck_benchmark_dataset_case_count"))
    op.create_index("ix_benchmark_dataset_revisions_workspace_id", "benchmark_dataset_revisions", ["workspace_id"])
    op.create_table("benchmark_dataset_cases", sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("dataset_revision_id", sa.UUID(), sa.ForeignKey("benchmark_dataset_revisions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("case_key", sa.String(64), nullable=False), sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("descriptor", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("dataset_revision_id", "case_key", name="uq_benchmark_dataset_case"),
        sa.UniqueConstraint("dataset_revision_id", "position", name="uq_benchmark_dataset_position"),
        sa.CheckConstraint("position >= 0", name="ck_benchmark_dataset_position"))
    op.create_index("ix_benchmark_dataset_cases_dataset_revision_id", "benchmark_dataset_cases", ["dataset_revision_id"])
    op.execute("""
        CREATE FUNCTION evalhive_immutable_dataset() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Installed benchmark dataset metadata is immutable';
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER immutable_benchmark_revision BEFORE UPDATE ON benchmark_dataset_revisions
        FOR EACH ROW EXECUTE FUNCTION evalhive_immutable_dataset();
    """)
    op.execute("""
        CREATE TRIGGER immutable_benchmark_case BEFORE UPDATE ON benchmark_dataset_cases
        FOR EACH ROW EXECUTE FUNCTION evalhive_immutable_dataset();
    """)


def downgrade():
    op.drop_table("benchmark_dataset_cases")
    op.drop_table("benchmark_dataset_revisions")
    op.execute("DROP FUNCTION evalhive_immutable_dataset()")
    op.drop_table("benchmark_secrets")
    op.drop_table("benchmark_profiles")
