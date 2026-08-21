"""Cost and token usage for the orchestrator's own LLM calls (SPA-111)

Revision ID: fac3d4e5f6a7
Revises: fab2c3d4e5f6
Create Date: 2026-08-21

Template selection, the decomposition decision and result evaluation each make an
LLM call, and none of them ever read `usage`. Agents were costed, judges were
costed, the orchestrator was not — so every cost figure the platform reported was
an undercount by an unknown margin, and the experiment budget cap only counted
part of what was being spent.

Stored apart from `cost_usd` / `token_usage` on purpose. Those two are the AGENT's
effort and feed the token-effort metric (SPA-77); folding orchestration overhead
into them would make an orchestrated run look like a wordier agent and quietly
corrupt the one axis that comparison is built on.

No backfill: `usage` was never read for these calls, so the tokens they spent were
never recorded anywhere and cannot be recovered. Historical rows keep a zero, which
is honestly what is known about them — the undercount is a property of runs made
before this migration, and the frozen corpus should not pretend otherwise.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "fac3d4e5f6a7"
down_revision = "fab2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "orchestrator_cost_usd",
            sa.Numeric(10, 6),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "tasks",
        sa.Column(
            "orchestrator_usage",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    # Denormalized onto the quality record too, next to the agent cost it sits
    # beside in every report roll-up.
    op.add_column(
        "quality_records",
        sa.Column(
            "orchestrator_cost_usd",
            sa.Numeric(10, 6),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("quality_records", "orchestrator_cost_usd")
    op.drop_column("tasks", "orchestrator_usage")
    op.drop_column("tasks", "orchestrator_cost_usd")
