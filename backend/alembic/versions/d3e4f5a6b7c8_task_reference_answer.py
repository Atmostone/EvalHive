"""task reference_answer — Reference-based Judge (E-03)

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-05-23
"""

from alembic import op
import sqlalchemy as sa


revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("reference_answer", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("tasks", "reference_answer")
