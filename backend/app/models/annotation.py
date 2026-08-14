import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AnnotationSession(Base):
    """The protocol one rating was collected under (E-05, SPA-85).

    Opened *before* anything is fetched, it declares its protocol and is handed
    one sanitized bundle built to match it; the rating is then submitted against
    it and the stored flags come from here, not from the request body.

    This is deliberately a claim about **what was served**, not about what the
    annotator has ever seen. Judge scores also reach a client from the on-demand
    evaluate endpoints, the experiment-results payload and the analytical
    surfaces, so «this person never saw the judge» is not a property the server
    can establish — and a partial version of it would read as a guarantee while
    being false.
    """

    __tablename__ = "annotation_sessions"
    __table_args__ = (
        Index("idx_annotation_sessions_user_task", "user_id", "task_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    protocol_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1"
    )
    blind_to_judge: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    blind_to_model: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Single-use: one bundle vouches for one rating. Without this, a blind bundle
    # could be opened once and then vouch for ratings made much later, after the
    # annotator had looked at the judge somewhere else.
    consumed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class Annotation(Base):
    """One rating of one run by one annotator — append-only (E-05, SPA-85).

    The system of record for human (and machine) ratings. ``quality_records
    .human_feedback`` remains as a materialised «latest human rating» for the
    existing readers, but it is a projection of this table, not the source.

    Two annotators rating the same run produce two independent rows, which is
    what makes inter-annotator agreement computable. The same annotator rating
    it again produces a new row pointing at the one it replaces
    (``supersedes_id``), so a re-rating never destroys the previous verdict and
    never inflates the population.

    ``judge_observation`` freezes what the judge had said at the moment of
    annotation, so a later re-judge cannot rewrite a past calibration.
    """

    __tablename__ = "annotations"
    __table_args__ = (
        # A chain, never a fork: at most one row may supersede any given row.
        UniqueConstraint("supersedes_id", name="uq_annotations_supersedes"),
        Index("idx_annotations_record", "quality_record_id"),
        Index("idx_annotations_task", "task_id"),
        Index("idx_annotations_workspace_type", "workspace_id", "annotator_type"),
        Index("idx_annotations_annotator", "annotator_id"),
        # One rating per session, enforced by the database rather than by a
        # read-then-write: two concurrent submissions must not both consume the
        # same bundle, and a retry has to find the row instead of writing a
        # second one under a different protocol.
        UniqueConstraint("session_id", name="uq_annotations_session"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    quality_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("quality_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Denormalized: scoping a population needs no join, and the row stays
    # readable in an export detached from its record.
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # human | llm_judge | synthetic | legacy — see ANNOTATOR_TYPES in
    # app/quality/feedback.py. What tools a person used while annotating is
    # deliberately not recorded; someone working with an assistant is `human`.
    annotator_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # Null for the machine types and for legacy rows (the old slot only ever
    # stored a display string). Honest `n_humans` counts distinct values here.
    annotator_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    annotator_label: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # The protocol this rating was collected under, recorded rather than
    # remembered. Blindness reflects what actually happened, not the intent.
    protocol_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1"
    )
    blind_to_model: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    blind_to_judge: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )

    verdict: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    overall_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dimensions: Mapped[list] = mapped_column(JSONB, nullable=False)
    judge_observation: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    supersedes_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotations.id", ondelete="SET NULL"), nullable=True
    )
    # The session that produced this rating — the evidence behind the blindness
    # flags above. Null for machine annotators, which declare their own protocol.
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("annotation_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
