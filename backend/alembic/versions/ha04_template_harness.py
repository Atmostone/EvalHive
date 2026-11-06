"""Select harness profiles and execution modes in templates.

Revision ID: ha04
Revises: ha03
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ha04"
down_revision = "ha03"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("templates", sa.Column("harness_profile_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_template_harness_profile", "templates", "harness_profiles", ["harness_profile_id"],
        ["id"], ondelete="RESTRICT",
    )
    op.add_column(
        "templates", sa.Column("execution_mode", sa.String(20), nullable=False, server_default="orchestrated")
    )
    op.create_check_constraint(
        "ck_template_execution_mode", "templates", "execution_mode IN ('direct', 'orchestrated')"
    )


def downgrade():
    op.drop_constraint("ck_template_execution_mode", "templates", type_="check")
    op.drop_column("templates", "execution_mode")
    op.drop_constraint("fk_template_harness_profile", "templates", type_="foreignkey")
    op.drop_column("templates", "harness_profile_id")
