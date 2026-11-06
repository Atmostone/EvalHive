"""Bind resource sessions and operation receipts to evaluator ownership.

Revision ID: bg08
Revises: bg07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "bg08"
down_revision = "bg07"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint("uq_evaluation_control_scope", "benchmark_evaluation_controls", ["id", "source_trial_id", "workspace_id"])
    op.add_column("benchmark_environment_sessions", sa.Column("control_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_benchmark_session_control", "benchmark_environment_sessions", "benchmark_evaluation_controls", ["control_id", "trial_id", "workspace_id"], ["id", "source_trial_id", "workspace_id"], ondelete="CASCADE")
    op.add_column("benchmark_operations", sa.Column("control_id", UUID(as_uuid=True), sa.ForeignKey("benchmark_evaluation_controls.id", ondelete="CASCADE"), nullable=True))
    op.drop_constraint("uq_benchmark_operation_identity", "benchmark_operations", type_="unique")
    op.create_unique_constraint("uq_benchmark_operation_identity", "benchmark_operations", ["trial_id", "control_id", "generation", "actor_id", "request_id"], postgresql_nulls_not_distinct=True)
    op.execute("""
        CREATE FUNCTION evalhive_frozen_resource_context() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.control_id IS DISTINCT FROM NEW.control_id THEN
                RAISE EXCEPTION 'Resource control context is immutable';
            END IF;
            IF TG_TABLE_NAME = 'benchmark_operations' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM benchmark_environment_sessions s WHERE s.id = NEW.session_id
                        AND s.trial_id = NEW.trial_id AND s.control_id IS NOT DISTINCT FROM NEW.control_id
                ) THEN
                    RAISE EXCEPTION 'Resource receipt control context differs from its session';
                END IF;
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    op.execute("CREATE TRIGGER frozen_resource_context BEFORE UPDATE ON benchmark_environment_sessions FOR EACH ROW EXECUTE FUNCTION evalhive_frozen_resource_context()")
    op.execute("CREATE TRIGGER frozen_resource_context BEFORE INSERT OR UPDATE ON benchmark_operations FOR EACH ROW EXECUTE FUNCTION evalhive_frozen_resource_context()")


def downgrade():
    # Preserve historical identities: rows that development data issued under
    # different contexts cannot be silently merged into one old receipt key.
    op.execute("DROP FUNCTION evalhive_frozen_resource_context() CASCADE")
    op.drop_constraint("uq_benchmark_operation_identity", "benchmark_operations", type_="unique")
    op.create_unique_constraint("uq_benchmark_operation_identity", "benchmark_operations", ["trial_id", "generation", "actor_id", "request_id"])
    op.drop_column("benchmark_operations", "control_id")
    op.drop_constraint("fk_benchmark_session_control", "benchmark_environment_sessions", type_="foreignkey")
    op.drop_column("benchmark_environment_sessions", "control_id")
    op.drop_constraint("uq_evaluation_control_scope", "benchmark_evaluation_controls", type_="unique")
