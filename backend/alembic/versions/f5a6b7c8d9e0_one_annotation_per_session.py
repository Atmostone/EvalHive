"""one annotation per session (SPA-85)

`annotations.session_id` carried no constraint, so «single-use» lived only in a
read-then-write in application code: two concurrent submissions could both claim
the same session, and one blind bundle could vouch for two ratings. The unique
index makes the database the arbiter, which is also what lets a retry after a
lost response be answered idempotently — the row is found rather than a second
one written under a silently different protocol.

Revision ID: f5a6b7c8d9e0
Revises: f3a4b5c6d7e8
Create Date: 2026-08-14
"""

from alembic import op

revision = "f5a6b7c8d9e0"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: ratings without a session (scripted annotators, pre-session rows)
    # are unconstrained, since NULLs do not collide in a unique index.
    op.drop_index("idx_annotations_session", table_name="annotations")
    op.create_unique_constraint(
        "uq_annotations_session", "annotations", ["session_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_annotations_session", "annotations", type_="unique")
    op.create_index("idx_annotations_session", "annotations", ["session_id"])
