"""Distinguish a contaminated CONDITION from a contaminated attempt (SPA-115)

Revision ID: fab2c3d4e5f6
Revises: faa1b2c3d4e5
Create Date: 2026-08-21

`failure_type` records that infrastructure decided a run's outcome. It does not
record whether that verdict outlives the attempt, and the two cases behave
oppositely on a re-queue: an agent-side quota error dies with the attempt, while
a degraded orchestrator that substituted and pinned a template changed the
condition every later attempt inherits.

The re-queue guard used to infer the second case from `template_id is not None`,
which is true of any orchestrated task at all — so an ordinary run that picked a
template and then hit a 429 kept its contamination forever, and its next,
successful run was dropped from every aggregate.

No backfill: the flag is a claim about how a contamination arose, and for rows
written before this column existed that provenance was never recorded. Defaulting
them to false makes a re-queue clear the reason, which is the safe direction —
the alternative silently excludes real measurements.
"""

import sqlalchemy as sa
from alembic import op

revision = "fab2c3d4e5f6"
down_revision = "faa1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "condition_contaminated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tasks", "condition_contaminated")
