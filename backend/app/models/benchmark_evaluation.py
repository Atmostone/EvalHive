"""An evaluator invocation has its own immutable inputs and terminal outcome."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BenchmarkEvaluationAttempt(Base):
    __tablename__ = "benchmark_evaluation_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["trial_id", "workspace_id"],
            ["benchmark_trials.id", "benchmark_trials.workspace_id"],
            ondelete="CASCADE",
            name="fk_benchmark_evaluation_scope",
        ),
        UniqueConstraint("id", "trial_id", "workspace_id", name="uq_benchmark_evaluation_scope"),
        ForeignKeyConstraint(
            ["source_evaluation_id", "trial_id", "workspace_id"],
            [
                "benchmark_evaluation_attempts.id",
                "benchmark_evaluation_attempts.trial_id",
                "benchmark_evaluation_attempts.workspace_id",
            ],
            deferrable=True,
            initially="DEFERRED",
            name="fk_benchmark_evaluation_source",
        ),
        CheckConstraint("origin_generation > 0", name="ck_benchmark_evaluation_generation"),
        CheckConstraint("kind IN ('initial','regrade')", name="ck_benchmark_evaluation_kind"),
        CheckConstraint(
            "status IN ('running','completed','error','cancelled','unavailable')",
            name="ck_benchmark_evaluation_status",
        ),
        CheckConstraint(
            "verdict IN ('correct','incorrect','unknown')", name="ck_benchmark_evaluation_verdict"
        ),
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND private_result IS NULL AND result_digest IS NULL) OR (status <> 'running' AND finished_at IS NOT NULL AND private_result IS NOT NULL AND result_digest IS NOT NULL)",
            name="ck_benchmark_evaluation_terminal",
        ),
        CheckConstraint(
            "(status = 'completed' AND ((success IS TRUE AND verdict = 'correct') OR (success IS FALSE AND verdict = 'incorrect') OR (success IS NULL AND verdict = 'unknown'))) OR (status <> 'completed' AND success IS NULL AND verdict = 'unknown')",
            name="ck_benchmark_evaluation_success",
        ),
        Index(
            "uq_benchmark_initial_evaluation",
            "trial_id",
            unique=True,
            postgresql_where=text("kind = 'initial'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    trial_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_evaluation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(30))
    origin_generation: Mapped[int] = mapped_column(Integer)
    controller_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    worker_actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    input_digest: Mapped[str] = mapped_column(String(64))
    private_inputs: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    status: Mapped[str] = mapped_column(String(30))
    verdict: Mapped[str] = mapped_column(String(30), default="unknown", server_default="unknown")
    success: Mapped[bool | None] = mapped_column(Boolean)
    metrics: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    primary_metric: Mapped[str | None] = mapped_column(String(200))
    error_kind: Mapped[str | None] = mapped_column(String(30))
    private_result: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    result_digest: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
