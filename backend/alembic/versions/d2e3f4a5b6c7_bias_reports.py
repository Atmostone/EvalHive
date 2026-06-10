"""bias reports — Bias Mitigation Toolkit (E-18)

Adds the bias_reports table: versioned before/after reports from the controlled
A/B re-judge of the calibration set (mitigations OFF vs ON). Each row holds the
two E-17 agreement passes plus per-bias diagnostics in the ``metrics`` JSONB,
versioned per (workspace_id, judge_config_key). Mirrors judge_calibrations.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-06-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bias_reports",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("judge_config_key", sa.String(255), nullable=False),
        sa.Column("judge_model", sa.String(255), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("sample_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("n_dimensions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("filters", JSONB(), nullable=False, server_default="{}"),
        sa.Column("metrics", JSONB(), nullable=False),
        sa.Column("threshold_kappa", sa.Float(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_by", sa.String(50), server_default="user"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("idx_bias_reports_workspace", "bias_reports", ["workspace_id"])
    op.create_index(
        "idx_bias_reports_key",
        "bias_reports",
        ["workspace_id", "judge_config_key"],
    )
    op.create_unique_constraint(
        "uq_bias_report_version",
        "bias_reports",
        ["workspace_id", "judge_config_key", "version"],
    )


def downgrade():
    op.drop_table("bias_reports")
