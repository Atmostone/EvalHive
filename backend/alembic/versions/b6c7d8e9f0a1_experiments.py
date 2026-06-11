"""experiments — Experiment Runner / A/B Matrix Harness (SPA-40)

Adds the experiments table (first-class experiment: frozen dataset × expanded
configuration matrix × n_runs_per_cell, cached report) and experiment_runs
(one row per matrix cell run, denormalized scores/cost so results survive task
deletion). Also adds tasks.origin ('user' | 'experiment') so benchmark
children can be excluded from the board by default.

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-06-11
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "b6c7d8e9f0a1"
down_revision = "a5b6c7d8e9f0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "experiments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("dataset", JSONB(), nullable=False),
        sa.Column("dataset_cases", JSONB(), nullable=False),
        sa.Column("matrix_spec", JSONB(), nullable=False),
        sa.Column("configurations", JSONB(), nullable=False),
        sa.Column("n_runs_per_cell", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("budget_limit_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("max_parallel", sa.Integer(), nullable=True),
        sa.Column("eval_config", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "accumulated_cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"
        ),
        sa.Column("report", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), server_default="user"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("idx_experiments_workspace", "experiments", ["workspace_id"])
    op.create_index("idx_experiments_status", "experiments", ["status"])
    op.create_unique_constraint(
        "uq_experiments_workspace_name", "experiments", ["workspace_id", "name"]
    )

    op.create_table(
        "experiment_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "experiment_id",
            UUID(as_uuid=True),
            sa.ForeignKey("experiments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("config_key", sa.String(64), nullable=False),
        sa.Column("case_key", sa.String(128), nullable=False),
        sa.Column("run_index", sa.Integer(), nullable=False),
        sa.Column(
            "task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("weighted_score", sa.Float(), nullable=True),
        sa.Column("trajectory_score", sa.Float(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "idx_experiment_runs_exp_status", "experiment_runs", ["experiment_id", "status"]
    )
    op.create_index("idx_experiment_runs_task", "experiment_runs", ["task_id"])
    op.create_unique_constraint(
        "uq_experiment_runs_cell",
        "experiment_runs",
        ["experiment_id", "config_key", "case_key", "run_index"],
    )

    op.add_column(
        "tasks",
        sa.Column("origin", sa.String(20), nullable=False, server_default="user"),
    )
    op.create_index("idx_tasks_workspace_origin", "tasks", ["workspace_id", "origin"])


def downgrade():
    op.drop_index("idx_tasks_workspace_origin", table_name="tasks")
    op.drop_column("tasks", "origin")
    op.drop_table("experiment_runs")
    op.drop_table("experiments")
