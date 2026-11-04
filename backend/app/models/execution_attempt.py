"""Execution evidence belongs to an attempt, independently of task retries."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "number", name="uq_execution_attempt_number"),
        Index("ix_execution_attempt_task_created", "task_id", "created_at"),
        CheckConstraint("number > 0 AND next_seq >= 0", name="ck_execution_attempt_counters"),
        Index(
            "uq_execution_attempt_active",
            "task_id",
            unique=True,
            postgresql_where=text("status IN ('created', 'running', 'stopping')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("harness_profiles.id", ondelete="SET NULL")
    )
    connector_id: Mapped[str] = mapped_column(String(200), nullable=False)
    connector_version: Mapped[str] = mapped_column(String(100), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(20), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    run_spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="created", server_default="created"
    )
    next_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    result: Mapped[dict | None] = mapped_column(JSONB)
    # Fullness describes recording, not model quality. Unexpected transport loss
    # remains explicit even when the model produced a plausible final message.
    completeness: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    container_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    last_heartbeat_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ExecutionEvent(Base):
    __tablename__ = "execution_events"
    __table_args__ = (
        UniqueConstraint("attempt_id", "event_key", name="uq_execution_event_identity"),
        UniqueConstraint("attempt_id", "seq", name="uq_execution_event_sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_attempts.id", ondelete="CASCADE"), nullable=False
    )
    # Hashes keep indexes bounded without shortening or rewriting native IDs.
    event_key: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ExecutionCommand(Base):
    __tablename__ = "execution_commands"
    __table_args__ = (Index("ix_execution_commands_pending", "attempt_id", "status", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("execution_attempts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="queued", server_default="queued"
    )
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    acknowledged_at: Mapped[datetime | None]
