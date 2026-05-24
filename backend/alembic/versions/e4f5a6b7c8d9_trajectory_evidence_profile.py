"""quality_records trajectory_evidence_profile — TRACE Evidence Bank Judge (E-08)

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-05-24
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "quality_records",
        sa.Column("trajectory_evidence_profile", JSONB(), nullable=True),
    )


def downgrade():
    op.drop_column("quality_records", "trajectory_evidence_profile")
