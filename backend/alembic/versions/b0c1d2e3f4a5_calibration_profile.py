"""calibration profile — Confidence Calibration (E-16)

Adds the calibration profile slot on the quality record, next to the
E-13/E-14/E-15 capability/failure/hallucination slots. The slot holds the
per-task (predicted_confidence, actual_correctness) pair plus its Brier term;
ECE / Brier / reliability-diagram metrics are computed at aggregate time.

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-05-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "b0c1d2e3f4a5"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade():
    # Per-task confidence-calibration pair (E-16), next to the other
    # quality slots.
    op.add_column(
        "quality_records",
        sa.Column("calibration_profile", JSONB(), nullable=True),
    )


def downgrade():
    op.drop_column("quality_records", "calibration_profile")
