"""Queue immutable benchmark launch requests independently of HTTP connections.

Revision ID: bg13
Revises: bg12
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "bg13"
down_revision = "bg12"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "benchmark_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trial_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_evaluation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("private_launch", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("controller_id", postgresql.UUID(as_uuid=True)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["trial_id", "workspace_id"],
            ["benchmark_trials.id", "benchmark_trials.workspace_id"],
            ondelete="CASCADE",
            name="fk_benchmark_job_scope",
        ),
        sa.ForeignKeyConstraint(
            ["source_evaluation_id", "trial_id", "workspace_id"],
            [
                "benchmark_evaluation_attempts.id",
                "benchmark_evaluation_attempts.trial_id",
                "benchmark_evaluation_attempts.workspace_id",
            ],
            name="fk_benchmark_job_evaluation",
        ),
        sa.UniqueConstraint("workspace_id", "request_id", name="uq_benchmark_job_request"),
        sa.CheckConstraint("kind IN ('execution','regrade')", name="ck_benchmark_job_kind"),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','error','cancelled')",
            name="ck_benchmark_job_status",
        ),
        sa.CheckConstraint(
            "(kind = 'execution' AND source_evaluation_id IS NULL) OR (kind = 'regrade' AND source_evaluation_id IS NOT NULL)",
            name="ck_benchmark_job_source",
        ),
        sa.CheckConstraint(
            "(status IN ('queued','running') AND finished_at IS NULL) OR (status IN ('completed','error','cancelled') AND finished_at IS NOT NULL)",
            name="ck_benchmark_job_terminal",
        ),
    )
    op.create_index("ix_benchmark_jobs_trial_id", "benchmark_jobs", ["trial_id"])
    op.create_index("ix_benchmark_jobs_dispatch", "benchmark_jobs", ["status", "created_at"])
    op.execute("""
        CREATE FUNCTION evalhive_frozen_benchmark_job() RETURNS trigger AS $$
        BEGIN
            IF ROW(OLD.id,OLD.workspace_id,OLD.trial_id,OLD.request_id,OLD.source_evaluation_id,
                   OLD.kind,OLD.request_digest,OLD.private_launch,OLD.deadline_at,OLD.created_at)
               IS DISTINCT FROM ROW(NEW.id,NEW.workspace_id,NEW.trial_id,NEW.request_id,NEW.source_evaluation_id,
                   NEW.kind,NEW.request_digest,NEW.private_launch,NEW.deadline_at,NEW.created_at) THEN
                RAISE EXCEPTION 'Benchmark launch inputs are immutable';
            END IF;
            IF OLD.cancel_requested_at IS NOT NULL AND OLD.cancel_requested_at IS DISTINCT FROM NEW.cancel_requested_at THEN
                RAISE EXCEPTION 'Benchmark launch cancellation is monotonic';
            END IF;
            IF OLD.status IN ('completed','error','cancelled') AND NEW IS DISTINCT FROM OLD THEN
                RAISE EXCEPTION 'Finished benchmark launch requests are immutable';
            END IF;
            IF OLD.status = 'running' AND NEW.status = 'queued' THEN
                RAISE EXCEPTION 'A started benchmark launch cannot be queued again';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE TRIGGER frozen_benchmark_job BEFORE UPDATE ON benchmark_jobs FOR EACH ROW EXECUTE FUNCTION evalhive_frozen_benchmark_job()"
    )


def downgrade():
    op.drop_table("benchmark_jobs")
    op.execute("DROP FUNCTION evalhive_frozen_benchmark_job()")
