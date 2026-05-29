"""hallucination profile — Hallucination Detection (E-15)

Adds the hallucination profile slot on the quality record, next to the
E-13/E-14 capability/failure slots. The slot holds a per-category fact-check
(urls / apis / numbers / citations) with a top-level hallucination_rate and a
per-category breakdown of checked vs. hallucinated items.

Revision ID: a9b0c1d2e3f4
Revises: f7a8b9c0d1e2
Create Date: 2026-05-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "a9b0c1d2e3f4"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade():
    # Per-category hallucination fact-check (E-15), next to the other
    # quality slots.
    op.add_column(
        "quality_records",
        sa.Column("hallucination_profile", JSONB(), nullable=True),
    )


def downgrade():
    op.drop_column("quality_records", "hallucination_profile")
