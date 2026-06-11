"""providers.max_concurrency — per-provider concurrent LLM call limit (SPA-47)

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-06-11
"""

import sqlalchemy as sa
from alembic import op

revision = "c7d8e9f0a1b2"
down_revision = "b6c7d8e9f0a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "providers", sa.Column("max_concurrency", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("providers", "max_concurrency")
