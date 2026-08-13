"""append-only annotation ledger (SPA-85)

``quality_records.human_feedback`` is a single JSONB slot that every save
overwrites, so a run could carry exactly one rating: inter-annotator agreement
was not computable from system data at all, and re-annotation destroyed the
previous verdict. Nothing recorded *who* rated — a person and an unattended LLM
annotator were indistinguishable — and nothing froze the judge's side, so a
re-judge silently rewrote a past calibration.

This adds the append-only ``annotations`` table. Each row records its
``annotator_type``, the protocol it was collected under (version + blindness),
and a snapshot of the judge observation as it stood at annotation time.
``supersedes_id`` chains a re-rating by the *same* annotator, while a different
annotator adds an independent row — which is what makes κ between annotators
fall out of the data.

The JSONB slot is kept as a materialised «latest human rating» so every existing
reader keeps working; it is no longer the system of record.

Existing rows migrate wholesale to ``legacy`` — no reconstruction, no per-row
forensics.

Revision ID: f2a3b4c5d6e7
Revises: f1a2b3c4d5e6
Create Date: 2026-08-13
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "f2a3b4c5d6e7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "annotations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "quality_record_id",
            UUID(as_uuid=True),
            sa.ForeignKey("quality_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Denormalized so scoping a population needs no join, and so the row
        # survives in an export detached from its record.
        sa.Column("task_id", UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        # 'human' | 'llm_judge' | 'synthetic' | 'legacy'. The distinction that
        # matters is whether a person decided or a model decided unattended —
        # what tools a person used while annotating is deliberately not a field.
        sa.Column("annotator_type", sa.String(20), nullable=False),
        # SET NULL: the rating outlives the account. Null for llm_judge /
        # synthetic / legacy, which have no user behind them.
        sa.Column(
            "annotator_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Display identity: the user's email, or the model's api_name.
        sa.Column("annotator_label", sa.String(255), nullable=True),
        # Properties of the collection protocol, recorded once rather than
        # remembered. Blindness is recorded as it actually was, not as intended.
        sa.Column("protocol_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "blind_to_model", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column(
            "blind_to_judge", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column("verdict", sa.String(20), nullable=True),
        sa.Column("overall_comment", sa.Text(), nullable=True),
        # Same element shape as the human_feedback slot's dimensions[].
        sa.Column("dimensions", JSONB(), nullable=False),
        # The judge's side frozen at annotation time — evaluator model, rubric,
        # schema version and the per-key scores of both judges. Without it the
        # calibration pair is rebuilt from a mutable profile.
        sa.Column("judge_observation", JSONB(), nullable=True),
        # A re-rating by the same annotator points at the row it replaces.
        # Unique, so the lineage is a chain and never forks.
        sa.Column(
            "supersedes_id",
            UUID(as_uuid=True),
            sa.ForeignKey("annotations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_unique_constraint(
        "uq_annotations_supersedes", "annotations", ["supersedes_id"]
    )
    op.create_index("idx_annotations_record", "annotations", ["quality_record_id"])
    op.create_index("idx_annotations_task", "annotations", ["task_id"])
    op.create_index(
        "idx_annotations_workspace_type", "annotations", ["workspace_id", "annotator_type"]
    )
    op.create_index("idx_annotations_annotator", "annotations", ["annotator_id"])

    # Every rating collected before this table existed becomes one `legacy` row.
    # It carries no annotator_id (the slot only ever stored a display string) and
    # no frozen judge observation — those are not reconstructable, and pretending
    # otherwise would be worse than labelling them. The regex guard on
    # submitted_at keeps a malformed value in a restored dump from aborting the
    # migration; such a row falls back to the record's own timestamp.
    op.execute(
        """
        INSERT INTO annotations (
            id, quality_record_id, task_id, workspace_id, annotator_type,
            annotator_id, annotator_label, protocol_version,
            blind_to_model, blind_to_judge, verdict, overall_comment,
            dimensions, judge_observation, supersedes_id, created_at
        )
        SELECT
            gen_random_uuid(), qr.id, qr.task_id, qr.workspace_id, 'legacy',
            NULL, qr.human_feedback->>'submitted_by', 1,
            false, false,
            qr.human_feedback->>'verdict', qr.human_feedback->>'overall_comment',
            COALESCE(qr.human_feedback->'dimensions', '[]'::jsonb), NULL, NULL,
            CASE
                WHEN qr.human_feedback->>'submitted_at'
                     ~ '^\\d{4}-\\d{2}-\\d{2}[T ]\\d{2}:\\d{2}:\\d{2}'
                THEN (qr.human_feedback->>'submitted_at')::timestamp
                ELSE qr.created_at
            END
        FROM quality_records qr
        WHERE qr.human_feedback IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("idx_annotations_annotator", table_name="annotations")
    op.drop_index("idx_annotations_workspace_type", table_name="annotations")
    op.drop_index("idx_annotations_task", table_name="annotations")
    op.drop_index("idx_annotations_record", table_name="annotations")
    op.drop_constraint("uq_annotations_supersedes", "annotations", type_="unique")
    op.drop_table("annotations")
