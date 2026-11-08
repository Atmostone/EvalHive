"""Retain immutable, workspace-scoped matrix and plan clone ancestry.

Revision ID: bg15
Revises: bg14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "bg15"
down_revision = "bg14"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("experiments", sa.Column("cloned_from_id", postgresql.UUID(as_uuid=True)))
    op.add_column("experiments", sa.Column("cloned_from_revision", sa.Integer()))
    op.add_column("experiments", sa.Column("clone_mode", sa.String(20)))
    op.create_foreign_key(
        "fk_experiment_clone_scope",
        "experiments",
        "experiments",
        ["cloned_from_id", "workspace_id"],
        ["id", "workspace_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_check_constraint(
        "ck_experiment_clone_origin",
        "experiments",
        "(cloned_from_id IS NULL AND cloned_from_revision IS NULL AND clone_mode IS NULL) OR "
        "(cloned_from_id IS NOT NULL AND cloned_from_id <> id AND cloned_from_revision IS NOT NULL AND clone_mode IS NOT NULL AND cloned_from_revision > 0 AND clone_mode IN ('frozen','new_conditions'))",
    )
    op.create_unique_constraint(
        "uq_benchmark_experiment_plan_id_scope",
        "benchmark_experiment_plans",
        ["id", "workspace_id"],
    )
    op.add_column(
        "benchmark_experiment_plans", sa.Column("source_plan_id", postgresql.UUID(as_uuid=True))
    )
    op.create_foreign_key(
        "fk_benchmark_plan_clone_scope",
        "benchmark_experiment_plans",
        "benchmark_experiment_plans",
        ["source_plan_id", "workspace_id"],
        ["id", "workspace_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_check_constraint(
        "ck_benchmark_plan_clone_self",
        "benchmark_experiment_plans",
        "source_plan_id IS NULL OR source_plan_id <> id",
    )
    op.execute("""
        CREATE FUNCTION guard_experiment_clone_origin() RETURNS trigger AS $$
        BEGIN
            IF (NEW.cloned_from_id, NEW.cloned_from_revision, NEW.clone_mode)
                IS DISTINCT FROM (OLD.cloned_from_id, OLD.cloned_from_revision, OLD.clone_mode) THEN
                RAISE EXCEPTION 'Experiment clone ancestry is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER experiment_clone_origin_immutable BEFORE UPDATE ON experiments
            FOR EACH ROW EXECUTE FUNCTION guard_experiment_clone_origin();
    """)


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM experiments WHERE cloned_from_id IS NOT NULL)
               OR EXISTS (SELECT 1 FROM benchmark_experiment_plans WHERE source_plan_id IS NOT NULL) THEN
                RAISE EXCEPTION 'Clone ancestry requires the paired database backup for rollback';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER experiment_clone_origin_immutable ON experiments")
    op.execute("DROP FUNCTION guard_experiment_clone_origin()")
    op.drop_constraint("ck_benchmark_plan_clone_self", "benchmark_experiment_plans", type_="check")
    op.drop_constraint(
        "fk_benchmark_plan_clone_scope", "benchmark_experiment_plans", type_="foreignkey"
    )
    op.drop_column("benchmark_experiment_plans", "source_plan_id")
    op.drop_constraint(
        "uq_benchmark_experiment_plan_id_scope", "benchmark_experiment_plans", type_="unique"
    )
    op.drop_constraint("ck_experiment_clone_origin", "experiments", type_="check")
    op.drop_constraint("fk_experiment_clone_scope", "experiments", type_="foreignkey")
    for name in ("clone_mode", "cloned_from_revision", "cloned_from_id"):
        op.drop_column("experiments", name)
