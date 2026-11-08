"""Persist reverse agent intent and confirmed receipts before SDK acknowledgements.

Revision ID: bg12
Revises: bg11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "bg12"
down_revision = "bg11"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "benchmark_agent_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("stream_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trial_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True)),
        sa.Column("request_id", sa.String(200), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("private_request", sa.LargeBinary(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("response_hash", sa.String(64)),
        sa.Column("private_response", sa.LargeBinary()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["stream_id", "trial_id"],
            ["benchmark_event_streams.id", "benchmark_event_streams.trial_id"],
            ondelete="CASCADE",
            name="fk_benchmark_agent_call_scope",
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["execution_attempts.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("stream_id", "request_id", name="uq_benchmark_agent_call_request"),
        sa.CheckConstraint(
            "operation IN ('run','open','control','events','close')",
            name="ck_benchmark_agent_call_operation",
        ),
        sa.CheckConstraint(
            "state IN ('started','completed','uncertain')", name="ck_benchmark_agent_call_state"
        ),
        sa.CheckConstraint(
            "(state = 'completed' AND private_response IS NOT NULL AND response_hash IS NOT NULL "
            "AND completed_at IS NOT NULL) OR (state <> 'completed' AND private_response IS NULL "
            "AND response_hash IS NULL AND completed_at IS NULL)",
            name="ck_benchmark_agent_call_receipt",
        ),
    )
    op.create_index("ix_benchmark_agent_calls_trial_id", "benchmark_agent_calls", ["trial_id"])
    op.create_index(
        "uq_benchmark_agent_start",
        "benchmark_agent_calls",
        ["stream_id"],
        unique=True,
        postgresql_where=sa.text("operation IN ('run','open')"),
    )
    op.execute("""
        CREATE FUNCTION evalhive_frozen_agent_call() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF ROW(OLD.id,OLD.stream_id,OLD.trial_id,OLD.session_id,OLD.request_id,OLD.operation,
                    OLD.content_hash,OLD.private_request,OLD.created_at)
                    IS DISTINCT FROM ROW(NEW.id,NEW.stream_id,NEW.trial_id,NEW.session_id,NEW.request_id,
                    NEW.operation,NEW.content_hash,NEW.private_request,NEW.created_at)
                    OR OLD.attempt_id IS NOT NULL AND OLD.attempt_id IS DISTINCT FROM NEW.attempt_id
                    OR OLD.state <> 'started' AND ROW(OLD.state,OLD.attempt_id,OLD.response_hash,
                    OLD.private_response,OLD.completed_at) IS DISTINCT FROM ROW(NEW.state,
                    NEW.attempt_id,NEW.response_hash,NEW.private_response,NEW.completed_at) THEN
                    RAISE EXCEPTION 'Agent call identity and final receipts are immutable';
                END IF;
            END IF;
            IF EXISTS (SELECT 1 FROM benchmark_trials WHERE id = NEW.trial_id AND phase = 'finished') THEN
                RAISE EXCEPTION 'Finished agent call journals are sealed';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE TRIGGER frozen_agent_call BEFORE INSERT OR UPDATE ON benchmark_agent_calls "
        "FOR EACH ROW EXECUTE FUNCTION evalhive_frozen_agent_call()"
    )


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM benchmark_agent_calls c JOIN benchmark_trials t
                ON t.id = c.trial_id WHERE t.phase <> 'finished') THEN
                RAISE EXCEPTION 'Finish benchmark agent sessions before downgrading';
            END IF;
        END $$;
    """)
    op.execute("DROP FUNCTION evalhive_frozen_agent_call() CASCADE")
    op.drop_table("benchmark_agent_calls")
