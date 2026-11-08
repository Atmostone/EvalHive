"""Track evaluator cleanup independently and seal its terminal lifecycle.

Revision ID: bg10
Revises: bg09
"""

import sqlalchemy as sa
from alembic import op

revision = "bg10"
down_revision = "bg09"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "benchmark_evaluation_controls",
        sa.Column("cleanup_status", sa.String(30), nullable=False, server_default="pending"),
    )
    op.add_column(
        "benchmark_evaluation_controls", sa.Column("finished_at", sa.DateTime(timezone=True))
    )
    op.execute(
        "UPDATE benchmark_evaluation_controls SET cleanup_status = 'complete', finished_at = clock_timestamp() WHERE phase = 'finished'"
    )
    op.create_check_constraint(
        "ck_evaluation_control_cleanup",
        "benchmark_evaluation_controls",
        "cleanup_status IN ('pending','running','complete','error')",
    )
    op.create_check_constraint(
        "ck_evaluation_control_finished",
        "benchmark_evaluation_controls",
        "(phase = 'finished') = (finished_at IS NOT NULL)",
    )
    op.execute("""
        CREATE FUNCTION evalhive_frozen_evaluator_lifecycle() RETURNS trigger AS $$
        BEGIN
            IF OLD.phase = 'finished' AND ROW(OLD.phase,OLD.finished_at,OLD.cleanup_status,OLD.recording_status)
                IS DISTINCT FROM ROW(NEW.phase,NEW.finished_at,NEW.cleanup_status,NEW.recording_status) THEN
                RAISE EXCEPTION 'Finished evaluation lifecycle is immutable';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE TRIGGER frozen_evaluator_lifecycle BEFORE UPDATE ON benchmark_evaluation_controls FOR EACH ROW EXECUTE FUNCTION evalhive_frozen_evaluator_lifecycle()"
    )


def downgrade():
    op.execute("DROP FUNCTION evalhive_frozen_evaluator_lifecycle() CASCADE")
    op.drop_constraint(
        "ck_evaluation_control_cleanup", "benchmark_evaluation_controls", type_="check"
    )
    op.drop_constraint(
        "ck_evaluation_control_finished", "benchmark_evaluation_controls", type_="check"
    )
    op.drop_column("benchmark_evaluation_controls", "cleanup_status")
    op.drop_column("benchmark_evaluation_controls", "finished_at")
