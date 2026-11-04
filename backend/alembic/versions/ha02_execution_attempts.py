"""Attempt-scoped execution journal and command/token identities.

Revision ID: ha02
Revises: ha01
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ha02"
down_revision = "ha01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "execution_attempts",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("task_id", sa.UUID(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.UUID(), sa.ForeignKey("harness_profiles.id", ondelete="SET NULL")),
        sa.Column("connector_id", sa.String(200), nullable=False),
        sa.Column("connector_version", sa.String(100), nullable=False),
        sa.Column("protocol_version", sa.String(20), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("run_spec", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="created"),
        sa.Column("next_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("completeness", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("container_id", sa.String(255)),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", "number", name="uq_execution_attempt_number"),
        sa.CheckConstraint("number > 0 AND next_seq >= 0", name="ck_execution_attempt_counters"),
    )
    op.create_index("ix_execution_attempts_workspace_id", "execution_attempts", ["workspace_id"])
    op.create_index("ix_execution_attempt_task_created", "execution_attempts", ["task_id", "created_at"])
    op.create_index("uq_execution_attempt_active", "execution_attempts", ["task_id"], unique=True,
                    postgresql_where=sa.text("status IN ('created', 'running', 'stopping')"))
    op.add_column("tasks", sa.Column("current_attempt_id", sa.UUID()))
    op.create_foreign_key("fk_task_current_attempt", "tasks", "execution_attempts", ["current_attempt_id"], ["id"], ondelete="SET NULL")
    op.add_column("service_tokens", sa.Column("attempt_id", sa.UUID()))
    op.create_foreign_key("fk_token_execution_attempt", "service_tokens", "execution_attempts", ["attempt_id"], ["id"], ondelete="CASCADE")
    op.create_table(
        "execution_events",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("attempt_id", sa.UUID(), sa.ForeignKey("execution_attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_key", sa.String(64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("event", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("attempt_id", "event_key", name="uq_execution_event_identity"),
        sa.UniqueConstraint("attempt_id", "seq", name="uq_execution_event_sequence"),
    )
    op.create_table(
        "execution_commands",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("attempt_id", sa.UUID(), sa.ForeignKey("execution_attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("acknowledged_at", sa.DateTime()),
    )
    op.create_index("ix_execution_commands_pending", "execution_commands", ["attempt_id", "status", "created_at"])


def downgrade():
    op.drop_constraint("fk_token_execution_attempt", "service_tokens", type_="foreignkey")
    op.drop_column("service_tokens", "attempt_id")
    op.drop_constraint("fk_task_current_attempt", "tasks", type_="foreignkey")
    op.drop_column("tasks", "current_attempt_id")
    op.drop_table("execution_commands")
    op.drop_table("execution_events")
    op.drop_table("execution_attempts")
