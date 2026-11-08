"""Own retained checkpoint files until resource cleanup completes.

Revision ID: bg11
Revises: bg10
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "bg11"
down_revision = "bg10"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "benchmark_evaluation_inputs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("control_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trial_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("origin_generation", sa.Integer(), nullable=False),
        sa.Column("controller_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_id", sa.String(200), nullable=False),
        sa.Column("reference_hash", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("private_input", sa.LargeBinary(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="reserved"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["control_id", "trial_id", "workspace_id"],
            [
                "benchmark_evaluation_controls.id",
                "benchmark_evaluation_controls.source_trial_id",
                "benchmark_evaluation_controls.workspace_id",
            ],
            name="fk_evaluation_input_scope",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("control_id", "artifact_id", name="uq_evaluation_input_artifact"),
        sa.CheckConstraint(
            "origin_generation > 0 AND size_bytes >= 0", name="ck_evaluation_input_size"
        ),
        sa.CheckConstraint(
            "state IN ('reserved','ready','released')", name="ck_evaluation_input_state"
        ),
        sa.CheckConstraint(
            "(state = 'released') = (released_at IS NOT NULL)", name="ck_evaluation_input_released"
        ),
    )
    op.create_index(
        "ix_benchmark_evaluation_inputs_control_id", "benchmark_evaluation_inputs", ["control_id"]
    )
    op.execute("""
        CREATE FUNCTION evalhive_frozen_evaluation_input() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF ROW(OLD.id,OLD.control_id,OLD.trial_id,OLD.workspace_id,OLD.session_id,
                    OLD.origin_generation,OLD.controller_id,OLD.artifact_id,OLD.reference_hash,
                    OLD.size_bytes,OLD.private_input,OLD.created_at)
                    IS DISTINCT FROM ROW(NEW.id,NEW.control_id,NEW.trial_id,NEW.workspace_id,NEW.session_id,
                    NEW.origin_generation,NEW.controller_id,NEW.artifact_id,NEW.reference_hash,
                    NEW.size_bytes,NEW.private_input,NEW.created_at)
                    OR OLD.state = 'released' AND ROW(OLD.state,OLD.released_at)
                        IS DISTINCT FROM ROW(NEW.state,NEW.released_at)
                    OR OLD.state = 'ready' AND NEW.state = 'reserved' THEN
                    RAISE EXCEPTION 'Evaluation input identity and released state are immutable';
                END IF;
            END IF;
            IF EXISTS (SELECT 1 FROM benchmark_evaluation_controls
                WHERE id = NEW.control_id AND phase = 'finished') THEN
                RAISE EXCEPTION 'Finished evaluation inputs are immutable';
            END IF;
            RETURN NEW;
        END; $$ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE TRIGGER frozen_evaluation_input BEFORE INSERT OR UPDATE ON benchmark_evaluation_inputs FOR EACH ROW EXECUTE FUNCTION evalhive_frozen_evaluation_input()"
    )


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM benchmark_evaluation_inputs WHERE state <> 'released') THEN
                RAISE EXCEPTION 'Release retained evaluation inputs before downgrading';
            END IF;
        END $$;
    """)
    op.execute("DROP FUNCTION evalhive_frozen_evaluation_input() CASCADE")
    op.drop_table("benchmark_evaluation_inputs")
