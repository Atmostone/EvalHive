"""Keep unreported execution cost unknown rather than storing a measured zero.

Revision ID: ha03
Revises: ha02
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op

revision = "ha03"
down_revision = "ha02"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("tasks", "quality_records"):
        op.alter_column(
            table,
            "cost_usd",
            existing_type=sa.Numeric(10, 6),
            type_=sa.Numeric(20, 12),
            nullable=True,
        )


def downgrade():
    connection = op.get_bind()
    for table in ("tasks", "quality_records"):
        if connection.scalar(
            sa.text(
                f"SELECT EXISTS (SELECT 1 FROM {table} WHERE cost_usd IS NULL OR cost_usd != round(cost_usd, 6) OR cost_usd >= 10000)"
            )
        ):
            raise RuntimeError(
                "Cannot downgrade unknown or precise execution costs without data loss; "
                "restore the compatible pre-release database backup for rollback"
            )
    for table in ("tasks", "quality_records"):
        op.alter_column(
            table,
            "cost_usd",
            existing_type=sa.Numeric(20, 12),
            type_=sa.Numeric(10, 6),
            nullable=False,
        )
