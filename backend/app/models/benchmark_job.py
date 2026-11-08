"""Durable product launch requests; trial ownership remains the execution authority."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BenchmarkJob(Base):
    __tablename__ = "benchmark_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["trial_id", "workspace_id"],
            ["benchmark_trials.id", "benchmark_trials.workspace_id"],
            ondelete="CASCADE",
            name="fk_benchmark_job_scope",
        ),
        ForeignKeyConstraint(
            ["source_evaluation_id", "trial_id", "workspace_id"],
            [
                "benchmark_evaluation_attempts.id",
                "benchmark_evaluation_attempts.trial_id",
                "benchmark_evaluation_attempts.workspace_id",
            ],
            name="fk_benchmark_job_evaluation",
        ),
        UniqueConstraint("workspace_id", "request_id", name="uq_benchmark_job_request"),
        CheckConstraint("kind IN ('execution','regrade')", name="ck_benchmark_job_kind"),
        CheckConstraint(
            "status IN ('queued','running','completed','error','cancelled')",
            name="ck_benchmark_job_status",
        ),
        CheckConstraint(
            "(kind = 'execution' AND source_evaluation_id IS NULL) OR (kind = 'regrade' AND source_evaluation_id IS NOT NULL)",
            name="ck_benchmark_job_source",
        ),
        CheckConstraint(
            "(status IN ('queued','running') AND finished_at IS NULL) OR (status IN ('completed','error','cancelled') AND finished_at IS NOT NULL)",
            name="ck_benchmark_job_terminal",
        ),
        Index("ix_benchmark_jobs_dispatch", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    trial_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_evaluation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(20))
    request_digest: Mapped[str] = mapped_column(String(64))
    private_launch: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued")
    controller_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
