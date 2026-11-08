"""Scoped reverse SDK calls; execution attempts remain the agent session record."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BenchmarkAgentCall(Base):
    __tablename__ = "benchmark_agent_calls"
    __table_args__ = (
        ForeignKeyConstraint(
            ["stream_id", "trial_id"],
            ["benchmark_event_streams.id", "benchmark_event_streams.trial_id"],
            ondelete="CASCADE",
            name="fk_benchmark_agent_call_scope",
        ),
        UniqueConstraint("stream_id", "request_id", name="uq_benchmark_agent_call_request"),
        Index(
            "uq_benchmark_agent_start",
            "stream_id",
            unique=True,
            postgresql_where=text("operation IN ('run','open')"),
        ),
        CheckConstraint(
            "operation IN ('run','open','control','events','close')",
            name="ck_benchmark_agent_call_operation",
        ),
        CheckConstraint(
            "state IN ('started','completed','uncertain')", name="ck_benchmark_agent_call_state"
        ),
        CheckConstraint(
            "(state = 'completed' AND private_response IS NOT NULL AND response_hash IS NOT NULL "
            "AND completed_at IS NOT NULL) OR (state <> 'completed' AND private_response IS NULL "
            "AND response_hash IS NULL AND completed_at IS NULL)",
            name="ck_benchmark_agent_call_receipt",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    stream_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    trial_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("execution_attempts.id", ondelete="RESTRICT")
    )
    request_id: Mapped[str] = mapped_column(String(200))
    operation: Mapped[str] = mapped_column(String(20))
    content_hash: Mapped[str] = mapped_column(String(64))
    private_request: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    state: Mapped[str] = mapped_column(String(20))
    response_hash: Mapped[str | None] = mapped_column(String(64))
    private_response: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
