"""record whether an annotator was served their peers' ratings (SPA-85)

Hiding other annotators' scores until you had rated left a hole: once you had,
they were revealed, and nothing stopped you re-rating afterwards. The collector
takes each annotator's *current* row, so the independent first rating was dropped
as superseded and the dependent re-rating took its place in the inter-annotator
κ — silently.

Sessions now never serve another annotator's opinion, and this column records
that rather than leaving it to be inferred from «has a session». Inter-annotator
agreement is computed only over ratings collected this way, so the number states
a property of its own population instead of assuming one.

Revision ID: f8a9b0c1d2e3
Revises: f5a6b7c8d9e0
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "f8a9b0c1d2e3"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Default false: a rating collected without a session — a script, or a client
    # that skipped the flow — makes no claim about what its author had seen.
    for table in ("annotation_sessions", "annotations"):
        op.add_column(
            table,
            sa.Column(
                "blind_to_peers", sa.Boolean(), nullable=False, server_default="false"
            ),
        )


def downgrade() -> None:
    op.drop_column("annotations", "blind_to_peers")
    op.drop_column("annotation_sessions", "blind_to_peers")
