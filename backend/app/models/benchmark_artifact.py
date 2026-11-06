"""Immutable evidence references; storage locations remain private host data."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BenchmarkArtifact(Base):
    __tablename__ = "benchmark_artifacts"
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
            name="fk_benchmark_artifact_scope",
        ),
        UniqueConstraint("session_id", "logical_id", name="uq_benchmark_artifact_identity"),
        CheckConstraint(
            "size_bytes >= 0 AND origin_generation > 0", name="ck_benchmark_artifact_size"
        ),
        CheckConstraint(
            "visibility IN ('agent','user','evaluator','private')",
            name="ck_benchmark_artifact_visibility",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    trial_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    origin_generation: Mapped[int] = mapped_column(Integer)
    logical_id: Mapped[str] = mapped_column(String(200))
    digest: Mapped[str] = mapped_column(String(71))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    visibility: Mapped[str] = mapped_column(String(30))
    media_type: Mapped[str] = mapped_column(String(200))
    private_locator: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
