"""perturbation runs — Adversarial / Perturbation Judge (E-12)

Adds the perturbation_runs table that groups a robustness probe of one finished
scenario: clean baseline re-runs plus perturbed re-runs per transform.

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-05-25
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "c4d5e6f7a8b9"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "perturbation_runs",
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
        sa.Column("template_id", UUID(as_uuid=True), nullable=True),
        sa.Column("transforms", JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "variants_per_transform", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("base_n", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("parallel", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("cost_cap_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("injection_canary", sa.String(64), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("base_task_ids", JSONB(), nullable=False, server_default="[]"),
        sa.Column("perturbed_task_ids", JSONB(), nullable=False, server_default="{}"),
        sa.Column("aggregate", JSONB(), nullable=True),
        sa.Column(
            "accumulated_cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "idx_perturbation_runs_workspace", "perturbation_runs", ["workspace_id"]
    )
    op.create_index(
        "idx_perturbation_runs_source", "perturbation_runs", ["source_task_id"]
    )
    op.create_index("idx_perturbation_runs_status", "perturbation_runs", ["status"])


def downgrade():
    op.drop_table("perturbation_runs")
