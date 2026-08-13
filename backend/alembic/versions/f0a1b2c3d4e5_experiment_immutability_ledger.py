"""experiment revisions + attempt ledger (SPA-84)

An experiment id has so far denoted a mutable folder rather than an immutable
statistical object: retry cleared a cell in place, add-config appended to a
finished experiment and remove-config deleted a config's lineage outright, none
of them leaving a trace. This adds the storage that makes each of those a
recorded event instead of an overwrite.

``experiment_runs`` stays the *current* state of a cell (its uniqueness on
experiment_id/config_key/case_key/run_index is unchanged, and every existing
query keeps working); ``experiment_attempts`` accumulates the superseded
executions of that cell.

``experiments.revision`` is bumped by every mutation, so a cached report can be
matched against the input it was computed from.

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "f0a1b2c3d4e5"
down_revision = "e9f0a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every mutation bumps this; a cached report records the revision it was
    # built from, so staleness becomes an equality check rather than a guess.
    op.add_column(
        "experiments",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    # Secondary guard: catches input changed without going through a mutation
    # (a hand-edited row, a restored dump) where the revision counter cannot.
    op.add_column(
        "experiments",
        sa.Column("input_fingerprint", sa.String(64), nullable=True),
    )

    # How many executions this cell has had, current one included. 0 = never run.
    op.add_column(
        "experiment_runs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    # Cells that ran before this column existed would otherwise read as "never
    # claimed", and the ledger write is skipped for those — so the first retry
    # of any pre-existing experiment would clear a real result with no record and
    # no error. 'pending' and 'skipped' are the only states a cell reaches
    # without ever having been claimed (skipped is stamped at the budget cap,
    # before the claim), and a task_id proves a claim regardless of status.
    op.execute(
        "UPDATE experiment_runs SET attempt_count = 1 "
        "WHERE task_id IS NOT NULL OR status NOT IN ('pending', 'skipped')"
    )
    # Set when the cell's configuration is retired. The row and its lineage stay;
    # the default report selection skips it.
    op.add_column(
        "experiment_runs",
        sa.Column("retired_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "experiment_attempts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "experiment_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("experiment_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 1-based, in execution order. The current cell state is attempt
        # attempt_count; rows here are the ones it superseded.
        sa.Column("attempt_index", sa.Integer(), nullable=False),
        # SET NULL, like experiment_runs.task_id: the denormalized scores below
        # outlive the task they came from.
        sa.Column(
            "task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("weighted_score", sa.Float(), nullable=True),
        sa.Column("trajectory_score", sa.Float(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("external_verdict", sa.Boolean(), nullable=True),
        sa.Column("launch_time", sa.String(64), nullable=True),
        sa.Column("lane_index", sa.Integer(), nullable=True),
        # Why this attempt stopped being current: 'retry' | 'config_retired'.
        sa.Column("retired_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_experiment_attempts_cell",
        "experiment_attempts",
        ["experiment_run_id", "attempt_index"],
    )
    op.create_index(
        "idx_experiment_attempts_run", "experiment_attempts", ["experiment_run_id"]
    )
    op.create_index("idx_experiment_attempts_task", "experiment_attempts", ["task_id"])


def downgrade() -> None:
    op.drop_index("idx_experiment_attempts_task", table_name="experiment_attempts")
    op.drop_index("idx_experiment_attempts_run", table_name="experiment_attempts")
    op.drop_constraint(
        "uq_experiment_attempts_cell", "experiment_attempts", type_="unique"
    )
    op.drop_table("experiment_attempts")
    op.drop_column("experiment_runs", "retired_at")
    op.drop_column("experiment_runs", "attempt_count")
    op.drop_column("experiments", "input_fingerprint")
    op.drop_column("experiments", "revision")
