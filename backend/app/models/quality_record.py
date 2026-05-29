import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class QualityRecord(Base):
    """Immutable, versioned snapshot of one task execution — the Quality Data Lake (E-01).

    One row per task (terminal status). Lightweight, queryable summary lives here;
    the full execution blob (decomposition tree, per-agent state snapshot, tool
    calls, events) is written to MinIO at `record_s3_path`. The JSONB slots are
    nullable placeholders filled by downstream features: quality_profile (E-02),
    trajectory_profile (E-07), trajectory_evidence_profile (E-08),
    trajectory_match_profile (E-09), capability_profile (E-13),
    failure_profile (E-14), hallucination_profile (E-15),
    human_feedback (E-05), longitudinal (E-22), reproducibility (E-20).
    """

    __tablename__ = "quality_records"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_quality_records_task"),
        Index("idx_quality_records_workspace", "workspace_id"),
        Index("idx_quality_records_template", "template_id"),
        Index("idx_quality_records_model", "model_used"),
        Index("idx_quality_records_status", "final_status"),
        Index("idx_quality_records_created", "created_at"),
        Index("idx_quality_records_benchmark_suite", "benchmark_suite"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Versioned schema for long-term use — the blob layout is tied to this.
    schema_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    # Denormalized so the record survives deletion/repricing of the source rows.
    template_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    template_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    model_used: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    final_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_decomposition_root: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # Benchmark Case Store linkage (denormalized from the task), for suite-scoped
    # aggregation that survives task deletion.
    benchmark_case_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    benchmark_suite: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Outcome metrics (denormalized from the task).
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal("0"), server_default="0"
    )
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tool_call_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Downstream slots (nullable placeholders).
    quality_profile: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    trajectory_profile: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    trajectory_evidence_profile: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    trajectory_match_profile: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    capability_profile: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    failure_profile: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    hallucination_profile: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    human_feedback: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    longitudinal: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    reproducibility: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    record_s3_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    public_dataset_opt_in: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
