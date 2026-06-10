"""ranking reports — Aggregation Engine (E-19)

Adds the ranking_reports table: versioned Bradley-Terry / Elo leaderboards built
from pairwise matches. Until the pairwise framework (E-21) exists the matches are
derived from pointwise quality scores; callers may also rank an explicit match
list. The full leaderboard (players with rating + bootstrap CI + win/loss/tie)
lives in the ``metrics`` JSONB, versioned per (workspace_id, ranking_key) where
ranking_key is "{subject}:{method}". Mirrors judge_calibrations / bias_reports.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-06-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "e3f4a5b6c7d8"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ranking_reports",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ranking_key", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(50), nullable=False),
        sa.Column("method", sa.String(50), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("n_players", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("n_matches", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("filters", JSONB(), nullable=False, server_default="{}"),
        sa.Column("metrics", JSONB(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_by", sa.String(50), server_default="user"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("idx_ranking_reports_workspace", "ranking_reports", ["workspace_id"])
    op.create_index(
        "idx_ranking_reports_key",
        "ranking_reports",
        ["workspace_id", "ranking_key"],
    )
    op.create_unique_constraint(
        "uq_ranking_report_version",
        "ranking_reports",
        ["workspace_id", "ranking_key", "version"],
    )


def downgrade():
    op.drop_table("ranking_reports")
