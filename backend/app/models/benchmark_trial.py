"""Durable benchmark ownership, journal, resource intents and operation receipts."""

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


class BenchmarkTrial(Base):
    __tablename__ = "benchmark_trials"
    __table_args__ = (
        CheckConstraint("generation > 0 AND next_seq >= 0", name="ck_benchmark_trial_counters"),
        CheckConstraint(
            "phase IN ('created','planning','preparing','ready','driving','capturing',"
            "'evaluating','cleaning','finished')",
            name="ck_benchmark_trial_phase",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    dataset_case_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("benchmark_dataset_cases.id", ondelete="RESTRICT")
    )
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("benchmark_profiles.id", ondelete="SET NULL")
    )
    retry_of_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("benchmark_trials.id", ondelete="SET NULL")
    )
    snapshot: Mapped[dict] = mapped_column(JSONB)
    generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    controller_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    controller_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    phase: Mapped[str] = mapped_column(String(30), default="created", server_default="created")
    execution_status: Mapped[str] = mapped_column(
        String(30), default="pending", server_default="pending"
    )
    evaluation_status: Mapped[str] = mapped_column(
        String(30), default="pending", server_default="pending"
    )
    recording_status: Mapped[str] = mapped_column(String(30), default="open", server_default="open")
    cleanup_status: Mapped[str] = mapped_column(
        String(30), default="pending", server_default="pending"
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BenchmarkEnvironmentSession(Base):
    __tablename__ = "benchmark_environment_sessions"
    __table_args__ = (
        UniqueConstraint(
            "id", "trial_id", "workspace_id", "origin_generation", name="uq_benchmark_session_scope"
        ),
        CheckConstraint("origin_generation > 0", name="ck_benchmark_session_generation"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trial_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benchmark_trials.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    origin_generation: Mapped[int] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(30), default="planned", server_default="planned")
    plan_digest: Mapped[str] = mapped_column(String(64))
    private_plan: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BenchmarkResourceLease(Base):
    __tablename__ = "benchmark_resource_leases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "trial_id", "workspace_id", "origin_generation"],
            [
                "benchmark_environment_sessions.id",
                "benchmark_environment_sessions.trial_id",
                "benchmark_environment_sessions.workspace_id",
                "benchmark_environment_sessions.origin_generation",
            ],
            ondelete="CASCADE",
            name="fk_benchmark_lease_scope",
        ),
        UniqueConstraint("session_id", "logical_id", name="uq_benchmark_lease_logical"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    trial_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    origin_generation: Mapped[int] = mapped_column(Integer)
    logical_id: Mapped[str] = mapped_column(String(200))
    provider_id: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(30))
    private_value: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BenchmarkOperation(Base):
    __tablename__ = "benchmark_operations"
    __table_args__ = (
        UniqueConstraint(
            "trial_id",
            "session_id",
            "generation",
            "actor_id",
            "request_id",
            name="uq_benchmark_operation_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trial_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benchmark_trials.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benchmark_environment_sessions.id", ondelete="CASCADE")
    )
    generation: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    request_id: Mapped[str] = mapped_column(String(200))
    content_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30))
    private_request: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    private_result: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_kind: Mapped[str | None] = mapped_column(String(30))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BenchmarkEventStream(Base):
    __tablename__ = "benchmark_event_streams"
    __table_args__ = (
        UniqueConstraint("trial_id", "generation", "actor_id", name="uq_benchmark_stream_actor"),
        CheckConstraint("next_seq >= 0", name="ck_benchmark_stream_sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trial_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benchmark_trials.id", ondelete="CASCADE")
    )
    generation: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    next_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    checkpoint_seq: Mapped[int | None] = mapped_column(BigInteger)
    final_seq: Mapped[int | None] = mapped_column(BigInteger)


class BenchmarkJournalEntry(Base):
    __tablename__ = "benchmark_journal"
    __table_args__ = (
        UniqueConstraint("trial_id", "seq", name="uq_benchmark_journal_seq"),
        UniqueConstraint("stream_id", "source_seq", name="uq_benchmark_journal_source_seq"),
        UniqueConstraint("stream_id", "event_id", name="uq_benchmark_journal_event_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trial_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benchmark_trials.id", ondelete="CASCADE"), index=True
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


class BenchmarkHostReservation(Base):
    __tablename__ = "benchmark_host_reservations"

    host_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    trial_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benchmark_trials.id", ondelete="RESTRICT"), unique=True
    )
    generation: Mapped[int] = mapped_column(Integer)
    controller_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    budget: Mapped[dict] = mapped_column(JSONB)
    # This reservation never expires automatically: native processes may outlive
    # the controller. Reconciliation must prove all resources released first.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
