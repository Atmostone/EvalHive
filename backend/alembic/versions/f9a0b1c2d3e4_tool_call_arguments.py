"""record tool-call arguments and call identity on agent log chunks (SPA-86)

The process judge is asked to score `parameter_quality` — whether the agent's
tool calls carried the right parameters — on a trace that never contained a
parameter. The agent parsed the arguments, called the tool with them and threw
them away; only the tool's name and its output were ever transmitted, so the
data lake had nothing to lose them from. The axis scored noise (ρ = 0.12 against
human re-rating) and `tool_selection` was judged on names alone.

`tool_call_id` + `part_index` fix a second, quieter defect: an output over the
transport cap is split across several rows, and the cleaner turned each row into
its own step — one call read as three, each capped separately and each counted
by the loop detector.

No backfill: the arguments were never recorded, so there is nothing to
reconstruct. Pre-existing rows keep NULL, which is the honest answer.

Revision ID: f9a0b1c2d3e4
Revises: f8a9b0c1d2e3
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f9a0b1c2d3e4"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_log_chunks",
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "agent_log_chunks",
        sa.Column(
            "arguments_truncated", sa.Boolean(), nullable=False, server_default="false"
        ),
    )
    op.add_column(
        "agent_log_chunks", sa.Column("tool_call_id", sa.String(length=128), nullable=True)
    )
    # Defaults describe a single-part output, which is what every existing row is.
    op.add_column(
        "agent_log_chunks",
        sa.Column("part_index", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agent_log_chunks",
        sa.Column("part_total", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("agent_log_chunks", "part_total")
    op.drop_column("agent_log_chunks", "part_index")
    op.drop_column("agent_log_chunks", "tool_call_id")
    op.drop_column("agent_log_chunks", "arguments_truncated")
    op.drop_column("agent_log_chunks", "arguments")
