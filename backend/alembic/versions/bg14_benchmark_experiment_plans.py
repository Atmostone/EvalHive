"""Freeze native matrix conditions separately from execution and public inputs.

Revision ID: bg14
Revises: bg13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "bg14"
down_revision = "bg13"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint("uq_experiments_scope", "experiments", ["id", "workspace_id"])
    op.create_table(
        "benchmark_experiment_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("experiment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_key", sa.String(64), nullable=False),
        sa.Column("case_key", sa.String(128), nullable=False),
        sa.Column("condition_digest", sa.String(64), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("private_inputs", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["experiment_id", "workspace_id"],
            ["experiments.id", "experiments.workspace_id"],
            ondelete="CASCADE",
            name="fk_benchmark_experiment_plan_scope",
        ),
        sa.UniqueConstraint(
            "experiment_id", "config_key", "case_key", name="uq_benchmark_experiment_plan_cell"
        ),
        sa.CheckConstraint(
            "condition_digest ~ '^[a-f0-9]{64}$'", name="ck_benchmark_experiment_plan_digest"
        ),
    )
    op.create_index(
        "ix_benchmark_experiment_plans_experiment_id",
        "benchmark_experiment_plans",
        ["experiment_id"],
    )
    op.execute("""
        CREATE FUNCTION guard_benchmark_experiment_plan() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Native benchmark experiment plans are immutable';
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER benchmark_experiment_plan_immutable
            BEFORE UPDATE ON benchmark_experiment_plans
            FOR EACH ROW EXECUTE FUNCTION guard_benchmark_experiment_plan();
    """)


def downgrade():
    op.drop_table("benchmark_experiment_plans")
    op.execute("DROP FUNCTION guard_benchmark_experiment_plan()")
    op.drop_constraint("uq_experiments_scope", "experiments", type_="unique")
