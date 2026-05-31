"""judge calibrations — Judge Calibration Protocol (E-17)

Adds the judge_calibrations table: versioned reports that validate the LLM judge
(E-02) against human feedback (E-05). Each row holds per-dimension agreement
(Pearson / Spearman / Cohen's kappa) plus an overall verdict-agreement in the
``metrics`` JSONB, versioned per (workspace_id, judge_config_key).

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-05-31
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "c1d2e3f4a5b6"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "judge_calibrations",
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
    op.create_index(
        "idx_judge_calibrations_workspace", "judge_calibrations", ["workspace_id"]
    )
    op.create_index(
        "idx_judge_calibrations_key",
        "judge_calibrations",
        ["workspace_id", "judge_config_key"],
    )
    op.create_unique_constraint(
        "uq_judge_calibration_version",
        "judge_calibrations",
        ["workspace_id", "judge_config_key", "version"],
    )


def downgrade():
    op.drop_table("judge_calibrations")
