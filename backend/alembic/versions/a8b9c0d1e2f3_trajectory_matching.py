"""trajectory matching — Trajectory Matching (E-09)

Adds the optional canonical (reference) trajectory on the task and the
deterministic match profile slot on the quality record.

Revision ID: a8b9c0d1e2f3
Revises: e4f5a6b7c8d9
Create Date: 2026-05-24
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "a8b9c0d1e2f3"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade():
    # Optional gold/canonical trajectory for tasks with a single valid path
    # (analogous to tasks.reference_answer from E-03). Non-null => canonical.
    op.add_column("tasks", sa.Column("canonical_trajectory", JSONB(), nullable=True))
    # Deterministic, LLM-free trajectory-match result (E-09), next to the
    # E-07/E-08 trajectory slots.
    op.add_column(
        "quality_records",
        sa.Column("trajectory_match_profile", JSONB(), nullable=True),
    )


def downgrade():
    op.drop_column("quality_records", "trajectory_match_profile")
    op.drop_column("tasks", "canonical_trajectory")
