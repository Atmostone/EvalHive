"""Separate immutable native evaluator attempts from execution outcomes.

Revision ID: bg06
Revises: bg05
"""

from alembic import op

revision = "bg06"
down_revision = "bg05"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint("uq_benchmark_trial_workspace", "benchmark_trials", ["id", "workspace_id"])
    op.execute("""
        CREATE TABLE benchmark_evaluation_attempts (
            id UUID PRIMARY KEY,
            trial_id UUID NOT NULL,
            workspace_id UUID NOT NULL,
            source_evaluation_id UUID,
            kind VARCHAR(30) NOT NULL CONSTRAINT ck_benchmark_evaluation_kind CHECK (kind IN ('initial','regrade')),
            origin_generation INTEGER NOT NULL CONSTRAINT ck_benchmark_evaluation_generation CHECK (origin_generation > 0),
            controller_id UUID NOT NULL,
            worker_actor_id UUID NOT NULL,
            input_digest VARCHAR(64) NOT NULL,
            private_inputs BYTEA NOT NULL,
            status VARCHAR(30) NOT NULL CONSTRAINT ck_benchmark_evaluation_status CHECK (status IN ('running','completed','error','cancelled','unavailable')),
            verdict VARCHAR(30) NOT NULL DEFAULT 'unknown' CONSTRAINT ck_benchmark_evaluation_verdict CHECK (verdict IN ('correct','incorrect','unknown')),
            success BOOLEAN,
            metrics JSONB NOT NULL DEFAULT '[]',
            primary_metric VARCHAR(200),
            error_kind VARCHAR(30),
            private_result BYTEA,
            result_digest VARCHAR(64),
            started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            CONSTRAINT fk_benchmark_evaluation_scope FOREIGN KEY (trial_id, workspace_id)
                REFERENCES benchmark_trials(id, workspace_id) ON DELETE CASCADE,
            CONSTRAINT uq_benchmark_evaluation_scope UNIQUE (id, trial_id, workspace_id),
            CONSTRAINT fk_benchmark_evaluation_source FOREIGN KEY (source_evaluation_id, trial_id, workspace_id)
                REFERENCES benchmark_evaluation_attempts(id, trial_id, workspace_id) DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT ck_benchmark_evaluation_terminal CHECK (
                (status = 'running' AND finished_at IS NULL AND private_result IS NULL AND result_digest IS NULL)
                OR (status <> 'running' AND finished_at IS NOT NULL AND private_result IS NOT NULL AND result_digest IS NOT NULL)
            ),
            CONSTRAINT ck_benchmark_evaluation_success CHECK (
                (status = 'completed' AND ((success IS TRUE AND verdict = 'correct') OR
                    (success IS FALSE AND verdict = 'incorrect') OR (success IS NULL AND verdict = 'unknown')))
                OR (status <> 'completed' AND success IS NULL AND verdict = 'unknown')
            )
        )
    """)
    op.execute("CREATE INDEX ix_benchmark_evaluation_attempts_trial_id ON benchmark_evaluation_attempts (trial_id)")
    op.execute("CREATE UNIQUE INDEX uq_benchmark_initial_evaluation ON benchmark_evaluation_attempts (trial_id) WHERE kind = 'initial'")
    op.execute("""
        CREATE FUNCTION evalhive_benchmark_frozen_evaluation() RETURNS trigger AS $$
        BEGIN
            IF ROW(OLD.id, OLD.trial_id, OLD.workspace_id, OLD.source_evaluation_id,
                   OLD.kind, OLD.origin_generation, OLD.controller_id, OLD.worker_actor_id, OLD.input_digest,
                   OLD.private_inputs, OLD.started_at) IS DISTINCT FROM
               ROW(NEW.id, NEW.trial_id, NEW.workspace_id, NEW.source_evaluation_id,
                   NEW.kind, NEW.origin_generation, NEW.controller_id, NEW.worker_actor_id, NEW.input_digest,
                   NEW.private_inputs, NEW.started_at) THEN
                RAISE EXCEPTION 'Benchmark evaluation inputs are immutable';
            END IF;
            IF OLD.status <> 'running' AND ROW(OLD.status, OLD.verdict, OLD.success, OLD.metrics,
                   OLD.primary_metric, OLD.error_kind, OLD.private_result, OLD.result_digest, OLD.finished_at)
               IS DISTINCT FROM ROW(NEW.status, NEW.verdict, NEW.success, NEW.metrics,
                   NEW.primary_metric, NEW.error_kind, NEW.private_result, NEW.result_digest, NEW.finished_at) THEN
                RAISE EXCEPTION 'Benchmark evaluation outcome is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER frozen_benchmark_evaluation BEFORE UPDATE ON benchmark_evaluation_attempts
        FOR EACH ROW EXECUTE FUNCTION evalhive_benchmark_frozen_evaluation();
    """)


def downgrade():
    op.execute("DROP FUNCTION evalhive_benchmark_frozen_evaluation() CASCADE")
    op.drop_table("benchmark_evaluation_attempts")
    op.drop_constraint("uq_benchmark_trial_workspace", "benchmark_trials", type_="unique")
