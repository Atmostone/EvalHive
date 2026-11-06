"""Bind broker request identity across environment sessions.

Revision ID: bg04
Revises: bg03
"""

from alembic import op

revision = "bg04"
down_revision = "bg03"
branch_labels = None
depends_on = None


def upgrade():
    # Previously issued IDs cannot be safely merged if development data reused
    # one actor/request across sessions. The unique constraint fails explicitly.
    op.drop_constraint("uq_benchmark_operation_identity", "benchmark_operations", type_="unique")
    op.create_unique_constraint(
        "uq_benchmark_operation_identity",
        "benchmark_operations",
        ["trial_id", "generation", "actor_id", "request_id"],
    )
    op.create_check_constraint(
        "ck_benchmark_operation_state",
        "benchmark_operations",
        "state IN ('started','complete','failed','uncertain')",
    )
    op.execute("""
        CREATE FUNCTION evalhive_benchmark_frozen_operation() RETURNS trigger AS $$
        BEGIN
            IF ROW(OLD.id, OLD.trial_id, OLD.session_id, OLD.generation, OLD.actor_id,
                   OLD.request_id, OLD.content_hash, OLD.private_request)
               IS DISTINCT FROM
               ROW(NEW.id, NEW.trial_id, NEW.session_id, NEW.generation, NEW.actor_id,
                   NEW.request_id, NEW.content_hash, NEW.private_request) THEN
                RAISE EXCEPTION 'Benchmark broker operation intent is immutable';
            END IF;
            IF OLD.private_result IS NOT NULL AND OLD.private_result IS DISTINCT FROM NEW.private_result THEN
                RAISE EXCEPTION 'Benchmark broker operation receipt is immutable';
            END IF;
            IF OLD.state <> 'started' AND ROW(OLD.state, OLD.private_result, OLD.error_code, OLD.error_kind, OLD.finished_at)
               IS DISTINCT FROM ROW(NEW.state, NEW.private_result, NEW.error_code, NEW.error_kind, NEW.finished_at) THEN
                RAISE EXCEPTION 'Benchmark broker operation outcome is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER frozen_benchmark_operation BEFORE UPDATE ON benchmark_operations
        FOR EACH ROW EXECUTE FUNCTION evalhive_benchmark_frozen_operation();
    """)


def downgrade():
    op.execute("DROP FUNCTION evalhive_benchmark_frozen_operation() CASCADE")
    op.drop_constraint("ck_benchmark_operation_state", "benchmark_operations", type_="check")
    op.drop_constraint("uq_benchmark_operation_identity", "benchmark_operations", type_="unique")
    op.create_unique_constraint(
        "uq_benchmark_operation_identity",
        "benchmark_operations",
        ["trial_id", "session_id", "generation", "actor_id", "request_id"],
    )
