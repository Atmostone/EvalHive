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
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_trial_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    generation: Mapped[int] = mapped_column(Integer)
    controller_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    controller_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    phase: Mapped[str] = mapped_column(String(30))
    next_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BenchmarkEvaluationJournal(Base):
    __tablename__ = "benchmark_evaluation_journal"
    __table_args__ = (UniqueConstraint("control_id", "seq", name="uq_evaluation_journal_sequence"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    control_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benchmark_evaluation_controls.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(BigInteger)
    generation: Mapped[int] = mapped_column(Integer)
    event: Mapped[str] = mapped_column(String(100))
    public_data: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    private_data: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
