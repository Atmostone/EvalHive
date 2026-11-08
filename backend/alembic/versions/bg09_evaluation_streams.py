"""Own worker streams, event delivery and command receipts per evaluator context.

Revision ID: bg09
Revises: bg08
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "bg09"
down_revision = "bg08"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_evaluation_control_trial", "benchmark_evaluation_controls", ["id", "source_trial_id"]
    )
    op.add_column(
        "benchmark_evaluation_controls",
        sa.Column("recording_status", sa.String(30), nullable=False, server_default="open"),
    )
    op.add_column(
        "benchmark_event_streams", sa.Column("control_id", UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_benchmark_stream_control",
        "benchmark_event_streams",
        "benchmark_evaluation_controls",
        ["control_id", "trial_id"],
        ["id", "source_trial_id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("uq_benchmark_stream_actor", "benchmark_event_streams", type_="unique")
    op.create_unique_constraint(
        "uq_benchmark_stream_actor",
        "benchmark_event_streams",
        ["trial_id", "control_id", "generation", "actor_id"],
        postgresql_nulls_not_distinct=True,
    )
    for column in [
        sa.Column(
            "stream_id",
            UUID(as_uuid=True),
            sa.ForeignKey("benchmark_event_streams.id", ondelete="CASCADE"),
        ),
        sa.Column("source_seq", sa.BigInteger()),
        sa.Column("event_id", sa.String(200)),
        sa.Column("content_hash", sa.String(64)),
    ]:
        op.add_column("benchmark_evaluation_journal", column)
    op.create_unique_constraint(
        "uq_evaluation_journal_source_seq",
        "benchmark_evaluation_journal",
        ["stream_id", "source_seq"],
    )
    op.create_unique_constraint(
        "uq_evaluation_journal_event_id", "benchmark_evaluation_journal", ["stream_id", "event_id"]
    )
    op.execute("""
        CREATE FUNCTION evalhive_stream_context_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_TABLE_NAME = 'benchmark_event_streams' THEN
                IF ROW(OLD.id,OLD.trial_id,OLD.control_id,OLD.generation,OLD.actor_id)
                   IS DISTINCT FROM ROW(NEW.id,NEW.trial_id,NEW.control_id,NEW.generation,NEW.actor_id) THEN
                    RAISE EXCEPTION 'Worker stream identity is immutable';
                END IF;
            ELSIF TG_TABLE_NAME = 'benchmark_journal' THEN
                IF NEW.stream_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM benchmark_event_streams s WHERE s.id=NEW.stream_id
                        AND s.control_id IS NULL AND s.trial_id=NEW.trial_id
                ) THEN
                    RAISE EXCEPTION 'Execution event stream context differs';
                END IF;
            ELSE
                IF NEW.stream_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM benchmark_event_streams s WHERE s.id=NEW.stream_id AND s.control_id=NEW.control_id
                ) THEN
                    RAISE EXCEPTION 'Evaluator event stream context differs';
                END IF;
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE TRIGGER frozen_stream_identity BEFORE UPDATE ON benchmark_event_streams FOR EACH ROW EXECUTE FUNCTION evalhive_stream_context_guard()"
    )
    for table in ("benchmark_journal", "benchmark_evaluation_journal"):
        op.execute(
            f"CREATE TRIGGER journal_stream_context BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION evalhive_stream_context_guard()"
        )


def downgrade():
    op.execute("DROP FUNCTION evalhive_stream_context_guard() CASCADE")
    op.drop_constraint(
        "uq_evaluation_journal_source_seq", "benchmark_evaluation_journal", type_="unique"
    )
    op.drop_constraint(
        "uq_evaluation_journal_event_id", "benchmark_evaluation_journal", type_="unique"
    )
    for column in ("stream_id", "source_seq", "event_id", "content_hash"):
        op.drop_column("benchmark_evaluation_journal", column)
    op.drop_constraint("uq_benchmark_stream_actor", "benchmark_event_streams", type_="unique")
    # An old schema cannot merge distinct historical context identities.
    op.create_unique_constraint(
        "uq_benchmark_stream_actor",
        "benchmark_event_streams",
        ["trial_id", "generation", "actor_id"],
    )
    op.drop_constraint("fk_benchmark_stream_control", "benchmark_event_streams", type_="foreignkey")
    op.drop_column("benchmark_event_streams", "control_id")
    op.drop_column("benchmark_evaluation_controls", "recording_status")
    op.drop_constraint(
        "uq_evaluation_control_trial", "benchmark_evaluation_controls", type_="unique"
    )
