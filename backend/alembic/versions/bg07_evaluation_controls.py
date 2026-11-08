"""Independent evaluator leases and immutable journals.

Revision ID: bg07
Revises: bg06
"""

from alembic import op

revision = "bg07"
down_revision = "bg06"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE benchmark_evaluation_controls (
            id UUID PRIMARY KEY, source_trial_id UUID NOT NULL, workspace_id UUID NOT NULL,
            generation INTEGER NOT NULL, controller_id UUID NOT NULL,
            controller_expires_at TIMESTAMPTZ NOT NULL, heartbeat_at TIMESTAMPTZ NOT NULL,
            deadline_at TIMESTAMPTZ NOT NULL, phase VARCHAR(30) NOT NULL,
            next_seq BIGINT NOT NULL DEFAULT 0, cancel_requested_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_evaluation_control_scope FOREIGN KEY (id,source_trial_id,workspace_id)
                REFERENCES benchmark_evaluation_attempts(id,trial_id,workspace_id) ON DELETE CASCADE,
            CONSTRAINT ck_evaluation_control_counters CHECK (generation > 0 AND next_seq >= 0),
            CONSTRAINT ck_evaluation_control_phase CHECK
                (phase IN ('created','planning','preparing','ready','capturing','evaluating','cleaning','finished'))
        )
    """)
    op.execute("""
        CREATE INDEX ix_benchmark_evaluation_controls_source_trial_id ON benchmark_evaluation_controls(source_trial_id)
    """)
    op.execute("""
        CREATE TABLE benchmark_evaluation_journal (
            id UUID PRIMARY KEY, control_id UUID NOT NULL REFERENCES benchmark_evaluation_controls(id) ON DELETE CASCADE,
            seq BIGINT NOT NULL, generation INTEGER NOT NULL, event VARCHAR(100) NOT NULL,
            public_data JSONB NOT NULL DEFAULT '{}', private_data BYTEA,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_evaluation_journal_sequence UNIQUE(control_id,seq)
        )
    """)
    op.execute("""
        CREATE INDEX ix_benchmark_evaluation_journal_control_id ON benchmark_evaluation_journal(control_id)
    """)
    op.execute("""
        CREATE TRIGGER immutable_evaluation_journal BEFORE UPDATE ON benchmark_evaluation_journal
            FOR EACH ROW EXECUTE FUNCTION evalhive_benchmark_immutable_journal()
    """)
    op.execute("""
        CREATE FUNCTION evalhive_frozen_evaluation_control() RETURNS trigger AS $$
        BEGIN
            IF ROW(OLD.id,OLD.source_trial_id,OLD.workspace_id,OLD.deadline_at,OLD.created_at)
               IS DISTINCT FROM ROW(NEW.id,NEW.source_trial_id,NEW.workspace_id,NEW.deadline_at,NEW.created_at) THEN
                RAISE EXCEPTION 'Evaluation control origin is immutable';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER frozen_evaluation_control BEFORE UPDATE ON benchmark_evaluation_controls
            FOR EACH ROW EXECUTE FUNCTION evalhive_frozen_evaluation_control();
    """)


def downgrade():
    op.drop_table("benchmark_evaluation_journal")
    op.drop_table("benchmark_evaluation_controls")
    op.execute("DROP FUNCTION evalhive_frozen_evaluation_control()")
