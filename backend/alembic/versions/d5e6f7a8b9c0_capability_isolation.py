"""capability isolation — Capability-isolation Tests (E-13, part A)

Adds the optional capability-isolation spec on the task (which tool(s) the task
cannot be solved without, plus its category) and the deterministic capability
profile slot on the quality record, next to the E-07/E-08/E-09 slots.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-05-26
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade():
    # Optional capability-isolation spec: {required_tools, category, match}.
    # Non-null => the task is a capability-isolation test (C1) and the
    # deterministic Glass-Box harness applies.
    op.add_column("tasks", sa.Column("capability_spec", JSONB(), nullable=True))
    # Deterministic capability-isolation result (E-13), next to the other
    # trajectory/quality slots.
    op.add_column(
        "quality_records",
        sa.Column("capability_profile", JSONB(), nullable=True),
    )


def downgrade():
    op.drop_column("quality_records", "capability_profile")
    op.drop_column("tasks", "capability_spec")
