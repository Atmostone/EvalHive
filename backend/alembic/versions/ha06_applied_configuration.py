"""Record when a connector's applied settings were frozen before execution.

Revision ID: ha06
Revises: ha05
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op

revision = "ha06"
down_revision = "ha05"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("execution_attempts", sa.Column("prepared_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("execution_attempts", "prepared_at")
