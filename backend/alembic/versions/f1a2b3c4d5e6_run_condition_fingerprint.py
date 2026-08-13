"""experiment_runs.condition_fingerprint — what each run ACTUALLY ran under (SPA-84)

The config-level ``resolved`` block records what a configuration meant when the
experiment started, but the runtime re-resolves at spawn: the engine reads
``template.soul_md`` live and ``run_config.model_id or template.model_id`` live.
A template edited mid-experiment therefore changes later runs of the same
``config_key`` without changing anything the config-level pin can see, so one
config key could quietly cover two different conditions.

This records the resolution per run, at claim time, which is the only place the
answer is authoritative.

Revision ID: f1a2b3c4d5e6
Revises: f0a1b2c3d4e5
Create Date: 2026-08-13
"""

import sqlalchemy as sa
from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL for rows spawned before this existed, and for rows never claimed.
    op.add_column(
        "experiment_runs",
        sa.Column("condition_fingerprint", sa.String(32), nullable=True),
    )
    # The ledger needs it too, or a retried cell loses the condition its earlier
    # attempt ran under — and a disagreement BETWEEN attempts of one cell is
    # exactly the case the per-run record exists to catch.
    op.add_column(
        "experiment_attempts",
        sa.Column("condition_fingerprint", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("experiment_attempts", "condition_fingerprint")
    op.drop_column("experiment_runs", "condition_fingerprint")
