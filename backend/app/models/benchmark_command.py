"""Private durable worker command intents, wire receipts and delivery barriers."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BenchmarkCommand(Base):
    __tablename__ = "benchmark_commands"
    __table_args__ = (
        ForeignKeyConstraint(
            ["stream_id", "trial_id"],
            ["benchmark_event_streams.id", "benchmark_event_streams.trial_id"],
            ondelete="CASCADE",
            name="fk_benchmark_command_scope",
        ),
        UniqueConstraint("stream_id", "request_id", name="uq_benchmark_command_request"),
        CheckConstraint(
            "state IN ('sent','received','completed','uncertain')",
            name="ck_benchmark_command_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trial_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    stream_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    request_id: Mapped[str] = mapped_column(String(200))
    method: Mapped[str] = mapped_column(String(40))
    phase: Mapped[str] = mapped_column(String(30))
    content_hash: Mapped[str] = mapped_column(String(64))
    private_request: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    state: Mapped[str] = mapped_column(String(30))
    response_hash: Mapped[str | None] = mapped_column(String(64))
    private_response: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    event_through_seq: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(100))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
