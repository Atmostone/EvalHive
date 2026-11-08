"""Durable benchmark worker commands and event delivery barriers.

Revision ID: bg03
Revises: bg02
"""

import sqlalchemy as sa
from alembic import op

revision = "bg03"
down_revision = "bg02"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_benchmark_stream_trial", "benchmark_event_streams", ["id", "trial_id"]
    )
    op.create_table(
        "benchmark_commands",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("trial_id", sa.UUID(), nullable=False),
        sa.Column("stream_id", sa.UUID(), nullable=False),
        sa.Column("request_id", sa.String(200), nullable=False),
        sa.Column("method", sa.String(40), nullable=False),
        sa.Column("phase", sa.String(30), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("private_request", sa.LargeBinary(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("response_hash", sa.String(64)),
        sa.Column("private_response", sa.LargeBinary()),
        sa.Column("event_through_seq", sa.BigInteger()),
        sa.Column("error_code", sa.String(100)),
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["stream_id", "trial_id"],
            ["benchmark_event_streams.id", "benchmark_event_streams.trial_id"],
            ondelete="CASCADE",
            name="fk_benchmark_command_scope",
        ),
        sa.UniqueConstraint("stream_id", "request_id", name="uq_benchmark_command_request"),
        sa.CheckConstraint(
            "state IN ('sent','received','completed','uncertain')",
            name="ck_benchmark_command_state",
        ),
    )
    op.create_index("ix_benchmark_commands_trial_id", "benchmark_commands", ["trial_id"])
    op.execute("""
        CREATE FUNCTION evalhive_benchmark_command_inputs() RETURNS trigger AS $$
        BEGIN
            IF ROW(OLD.id, OLD.trial_id, OLD.stream_id, OLD.request_id, OLD.method,
                   OLD.phase, OLD.content_hash, OLD.private_request)
               IS DISTINCT FROM
               ROW(NEW.id, NEW.trial_id, NEW.stream_id, NEW.request_id, NEW.method,
                   NEW.phase, NEW.content_hash, NEW.private_request) THEN
                RAISE EXCEPTION 'Benchmark command intent is immutable';
            END IF;
            IF OLD.private_response IS NOT NULL AND ROW(OLD.private_response, OLD.response_hash)
               IS DISTINCT FROM ROW(NEW.private_response, NEW.response_hash) THEN
                RAISE EXCEPTION 'Benchmark command response is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER frozen_benchmark_command BEFORE UPDATE ON benchmark_commands
        FOR EACH ROW EXECUTE FUNCTION evalhive_benchmark_command_inputs();
    """)


def downgrade():
    op.drop_table("benchmark_commands")
    op.execute("DROP FUNCTION evalhive_benchmark_command_inputs()")
    op.drop_constraint("uq_benchmark_stream_trial", "benchmark_event_streams", type_="unique")
