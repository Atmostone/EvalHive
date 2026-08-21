"""Store the model's own deliberation, apart from what it said (SPA-114)

Revision ID: fad4e5f6a7b8
Revises: fac3d4e5f6a7
Create Date: 2026-08-21

A reasoning model returns its thinking in a field separate from the answer, and
nothing in this repository read it — `grep -rn "reasoning_content"` returned
zero. Confirmed live on MiniMax-M3: of 30 output tokens on a one-word reply, ~25
went to reasoning the platform discarded.

The process judge is then asked how the agent worked — efficiency, tool
selection, error recovery, goal alignment — every one of which, on such a model,
is answered largely in the stream that was thrown away. Same shape as SPA-86
(arguments were never recorded, so `parameter_quality` had no subject) and
SPA-113 (the trace was not in the order things happened): the recording layer was
at fault and the model was charged for it.

Its own column, never merged into `content`: mixing them would silently change
what every existing consumer reads, and «the model's private deliberation» vs
«what it said» is precisely the distinction worth keeping.

No backfill — the field was never requested from any provider, so for every
existing chunk the deliberation is simply gone.
"""

import sqlalchemy as sa
from alembic import op

revision = "fad4e5f6a7b8"
down_revision = "fac3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_log_chunks", sa.Column("reasoning", sa.Text(), nullable=True))
    # A SUBSET of output_tokens, denormalized beside it. Nullable rather than 0:
    # a model that does not reason and a provider that does not report the split
    # are both "unknown", and a zero would let them read as "thought nothing".
    op.add_column(
        "quality_records", sa.Column("reasoning_tokens", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("quality_records", "reasoning_tokens")
    op.drop_column("agent_log_chunks", "reasoning")
