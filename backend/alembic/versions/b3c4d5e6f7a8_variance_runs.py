"""variance runs — Variance / Robustness Harness + re-run core (E-11)

Adds the re-run lineage/override columns on tasks and the variance_runs table
that groups N re-runs of one scenario.

Revision ID: b3c4d5e6f7a8
Revises: a8b9c0d1e2f3
Create Date: 2026-05-25
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "b3c4d5e6f7a8"
down_revision = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade():
    # Re-run core (layer A): lineage + per-run override seam on tasks.
    op.add_column(
        "tasks",
        sa.Column("replay_of_task_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_tasks_replay_of",
        "tasks",
        "tasks",
        ["replay_of_task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_tasks_replay_of", "tasks", ["replay_of_task_id"])
    op.add_column("tasks", sa.Column("run_config", JSONB(), nullable=True))

    # Variance / Robustness Harness (E-11).
    op.create_table(
        "variance_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_spec", JSONB(), nullable=True),
        sa.Column("template_id", UUID(as_uuid=True), nullable=True),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("parallel", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("cost_cap_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("child_task_ids", JSONB(), nullable=False, server_default="[]"),
        sa.Column("aggregate", JSONB(), nullable=True),
        sa.Column(
            "accumulated_cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("idx_variance_runs_workspace", "variance_runs", ["workspace_id"])
    op.create_index("idx_variance_runs_source", "variance_runs", ["source_task_id"])
    op.create_index("idx_variance_runs_status", "variance_runs", ["status"])


def downgrade():
    op.drop_table("variance_runs")
    op.drop_column("tasks", "run_config")
    op.drop_index("idx_tasks_replay_of", table_name="tasks")
    op.drop_constraint("fk_tasks_replay_of", "tasks", type_="foreignkey")
    op.drop_column("tasks", "replay_of_task_id")
