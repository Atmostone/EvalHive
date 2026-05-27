"""failure modes — Failure Mode Classifier (E-14)

Adds the failure-mode profile slot on the quality record, next to the
E-07/E-08/E-09/E-13 trajectory/quality slots. The slot holds a multi-label set
of failure classes (tool confusion, parameter-blind, loop, premature stop,
hallucinated tool result, ignored error) with per-label confidence and reason.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-05-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "f7a8b9c0d1e2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade():
    # Multi-label failure-mode classification (E-14), next to the other
    # trajectory/quality slots.
    op.add_column(
        "quality_records",
        sa.Column("failure_profile", JSONB(), nullable=True),
    )


def downgrade():
    op.drop_column("quality_records", "failure_profile")
