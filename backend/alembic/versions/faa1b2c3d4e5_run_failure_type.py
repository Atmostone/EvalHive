"""record WHY a run failed, as a type (SPA-87)

`status = "failed"` had twelve-odd writers and one value. A provider quota, a
dead API key, a container that never came up, a max-iteration cap and a model
giving up were indistinguishable in SQL: the only trace of the reason was a
free-text string inside an `agent_events` JSONB payload, which no query and no
report ever read.

That mattered once the cap-hit fix (SPA-70) started scoring non-verifiable runs
regardless of terminal status. A quota-killed run has no deliverables, so it now
earns a genuine low score and enters the analysis as a real data point. The NULL
score used to filter it out by accident; nothing filtered it after.

`failure_type` is carried on the task (where the agent's webhook lands) and
denormalised onto the run and its superseded attempts (where the report reads).
NULL means «not classified» — an older agent image, a path that never reported —
and is treated as ordinary data, never as clean and never as contaminated. No
backfill: the reason was never stored, so there is nothing to recover.

Revision ID: faa1b2c3d4e5
Revises: f9a0b1c2d3e4
Create Date: 2026-08-17
"""

import sqlalchemy as sa
from alembic import op

revision = "faa1b2c3d4e5"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("failure_type", sa.String(length=32), nullable=True))
    op.add_column(
        "experiment_runs", sa.Column("failure_type", sa.String(length=32), nullable=True)
    )
    # The attempt ledger is a shadow of experiment_runs: a column missing here is
    # silently lost on every retry, and reads back as the model default under the
    # all_attempts report selection.
    op.add_column(
        "experiment_attempts", sa.Column("failure_type", sa.String(length=32), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("experiment_attempts", "failure_type")
    op.drop_column("experiment_runs", "failure_type")
    op.drop_column("tasks", "failure_type")
