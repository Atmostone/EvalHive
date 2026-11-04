"""Preserve unreported and sub-picodollar harness costs throughout results.

Revision ID: ha08
Revises: ha07
"""

import sqlalchemy as sa
from alembic import op

revision = "ha08"
down_revision = "ha07"
branch_labels = None
depends_on = None

TABLES = {
    "tasks": (20, 12),
    "quality_records": (20, 12),
    "experiment_runs": (10, 6),
    "experiment_attempts": (10, 6),
}


def upgrade():
    op.alter_column("experiments", "accumulated_cost_usd", existing_type=sa.Numeric(10, 6), type_=sa.Numeric())
    for table, shape in TABLES.items():
        op.alter_column(table, "cost_usd", existing_type=sa.Numeric(*shape),
                        type_=sa.Numeric(), nullable=True)


def downgrade():
    connection = op.get_bind()
    entries = {**TABLES, "experiments": (10, 6)}
    for table, (precision, scale) in entries.items():
        column = "accumulated_cost_usd" if table == "experiments" else "cost_usd"
        nonnull = table in {"experiment_runs", "experiment_attempts"}
        if connection.scalar(sa.text(
            f"SELECT EXISTS (SELECT 1 FROM {table} WHERE "
            f"{column} != round({column}, {scale}) OR abs({column}) >= power(10, {precision - scale})"
            + (" OR cost_usd IS NULL" if nonnull else "") + ")"
        )):
            raise RuntimeError("Cost downgrade would lose measured precision or unknown values; restore the compatible backup")
    op.alter_column("experiments", "accumulated_cost_usd", existing_type=sa.Numeric(), type_=sa.Numeric(10, 6))
    for table, shape in TABLES.items():
        op.alter_column(table, "cost_usd", existing_type=sa.Numeric(), type_=sa.Numeric(*shape),
                        nullable=table not in {"experiment_runs", "experiment_attempts"})
