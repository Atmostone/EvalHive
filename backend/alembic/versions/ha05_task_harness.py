"""Choose a harness profile and optional execution mode for each task.

Revision ID: ha05
Revises: ha04
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ha05"
down_revision = "ha04"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("harness_profile_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_task_harness_profile", "tasks", "harness_profiles", ["harness_profile_id"],
        ["id"], ondelete="RESTRICT",
    )
    op.add_column("tasks", sa.Column("execution_mode", sa.String(20)))
    op.create_check_constraint(
        "ck_task_execution_mode", "tasks", "execution_mode IN ('direct', 'orchestrated')"
    )


def downgrade():
    op.drop_constraint("ck_task_execution_mode", "tasks", type_="check")
    op.drop_column("tasks", "execution_mode")
    op.drop_constraint("fk_task_harness_profile", "tasks", type_="foreignkey")
    op.drop_column("tasks", "harness_profile_id")
