"""Identify the attempt measured by each newly archived quality record.

Revision ID: ha07
Revises: ha06
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ha07"
down_revision = "ha06"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("quality_records", sa.Column("execution_attempt_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_quality_record_execution_attempt", "quality_records", "execution_attempts",
        ["execution_attempt_id"], ["id"], ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint("fk_quality_record_execution_attempt", "quality_records", type_="foreignkey")
    op.drop_column("quality_records", "execution_attempt_id")
