"""Immutable benchmark evidence references.

Revision ID: bg05
Revises: bg04
"""

from alembic import op

revision = "bg05"
down_revision = "bg04"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE benchmark_artifacts (
            id UUID PRIMARY KEY,
            session_id UUID NOT NULL,
            trial_id UUID NOT NULL,
            workspace_id UUID NOT NULL,
            origin_generation INTEGER NOT NULL,
            logical_id VARCHAR(200) NOT NULL,
            digest VARCHAR(71) NOT NULL,
            size_bytes BIGINT NOT NULL,
            visibility VARCHAR(30) NOT NULL,
            media_type VARCHAR(200) NOT NULL,
            private_locator BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_benchmark_artifact_scope FOREIGN KEY
                (session_id, trial_id, workspace_id, origin_generation) REFERENCES
                benchmark_environment_sessions (id, trial_id, workspace_id, origin_generation)
                ON DELETE CASCADE,
            CONSTRAINT uq_benchmark_artifact_identity UNIQUE (session_id, logical_id),
            CONSTRAINT ck_benchmark_artifact_size CHECK (size_bytes >= 0 AND origin_generation > 0),
            CONSTRAINT ck_benchmark_artifact_visibility CHECK
                (visibility IN ('agent','user','evaluator','private'))
        )
    """)
    op.execute("CREATE INDEX ix_benchmark_artifacts_trial_id ON benchmark_artifacts (trial_id)")
    op.execute("""
        CREATE TRIGGER immutable_benchmark_artifact BEFORE UPDATE ON benchmark_artifacts
        FOR EACH ROW EXECUTE FUNCTION evalhive_benchmark_immutable_journal();
    """)


def downgrade():
    op.drop_table("benchmark_artifacts")
