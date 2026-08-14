"""annotation sessions — the protocol an annotation was collected under (SPA-85)

The blindness flags were first derived by tracking every judge score the server
had ever shown a user. That cannot be made true: scores also reach the client
from the on-demand evaluate endpoints, the whole experiment-results payload and
every analytical surface, so the tracking was necessarily partial — and a partial
guarantee is worse than none, because it reads as a guarantee.

So the claim is narrowed to something the server can actually prove. An
annotation session is opened *before* anything is fetched, declares its protocol,
and is handed one sanitized bundle built to match it. The rating is submitted
against that session, and the stored flags come from the session row rather than
from the request body. `blind_to_judge` therefore means «this rating was produced
through a session that was served no judge scores» — a fact about what was
served, not a claim about everything the person has ever seen.

Revision ID: f3a4b5c6d7e8
Revises: f2a3b4c5d6e7
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "f3a4b5c6d7e8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "annotation_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        # SET NULL, like the annotation's own annotator: the protocol record
        # outlives the account that used it.
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("protocol_version", sa.Integer(), nullable=False, server_default="1"),
        # What the bundle served under this session did NOT contain.
        sa.Column(
            "blind_to_judge", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column(
            "blind_to_model", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        # Stamped when a rating is submitted against it. A session is single-use:
        # re-submitting would let one blind bundle vouch for later ratings made
        # after the annotator had looked elsewhere.
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "idx_annotation_sessions_user_task", "annotation_sessions", ["user_id", "task_id"]
    )

    # Which session produced this rating — null for machine annotators, which
    # declare their own protocol, and for pre-session rows.
    op.add_column(
        "annotations",
        sa.Column(
            "session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("annotation_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("idx_annotations_session", "annotations", ["session_id"])


def downgrade() -> None:
    op.drop_index("idx_annotations_session", table_name="annotations")
    op.drop_column("annotations", "session_id")
    op.drop_index("idx_annotation_sessions_user_task", table_name="annotation_sessions")
    op.drop_table("annotation_sessions")
