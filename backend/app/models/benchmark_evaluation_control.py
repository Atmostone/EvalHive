"""Mutable evaluator ownership is separate from immutable invocation inputs/results."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BenchmarkEvaluationControl(Base):
    __tablename__ = "benchmark_evaluation_controls"
    __table_args__ = (
        UniqueConstraint("id", "source_trial_id", name="uq_evaluation_control_trial"),
        UniqueConstraint(
            "id", "source_trial_id", "workspace_id", name="uq_evaluation_control_scope"
        ),
        ForeignKeyConstraint(
            ["id", "source_trial_id", "workspace_id"],
            [
                "benchmark_evaluation_attempts.id",
                "benchmark_evaluation_attempts.trial_id",
                "benchmark_evaluation_attempts.workspace_id",
            ],
            ondelete="CASCADE",
            name="fk_evaluation_control_scope",
        ),
        CheckConstraint("generation > 0 AND next_seq >= 0", name="ck_evaluation_control_counters"),
        CheckConstraint(
            "phase IN ('created','planning','preparing','ready','capturing','evaluating','cleaning','finished')",
            name="ck_evaluation_control_phase",
        ),
        CheckConstraint(
            "cleanup_status IN ('pending','running','complete','error')",
            name="ck_evaluation_control_cleanup",
        ),
        CheckConstraint(
            "(phase = 'finished') = (finished_at IS NOT NULL)",
            name="ck_evaluation_control_finished",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_trial_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    generation: Mapped[int] = mapped_column(Integer)
    controller_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    controller_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recording_status: Mapped[str] = mapped_column(String(30), default="open", server_default="open")
    cleanup_status: Mapped[str] = mapped_column(
        String(30), default="pending", server_default="pending"
    )
    phase: Mapped[str] = mapped_column(String(30))
    next_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BenchmarkEvaluationJournal(Base):
    __tablename__ = "benchmark_evaluation_journal"
    __table_args__ = (
        UniqueConstraint("control_id", "seq", name="uq_evaluation_journal_sequence"),
        UniqueConstraint("stream_id", "source_seq", name="uq_evaluation_journal_source_seq"),
        UniqueConstraint("stream_id", "event_id", name="uq_evaluation_journal_event_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    control_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benchmark_evaluation_controls.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(BigInteger)
    generation: Mapped[int] = mapped_column(Integer)
    stream_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("benchmark_event_streams.id", ondelete="CASCADE")
    )
    source_seq: Mapped[int | None] = mapped_column(BigInteger)
    event_id: Mapped[str | None] = mapped_column(String(200))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(100))
    public_data: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    private_data: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
