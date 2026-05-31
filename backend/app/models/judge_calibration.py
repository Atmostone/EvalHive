import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class JudgeCalibration(Base):
    """A versioned judge-calibration report — Judge Calibration Protocol (E-17).

    Each row is one validation of the LLM judge (E-02) against human feedback
    (E-05): per-dimension agreement (Pearson / Spearman / Cohen's kappa) plus an
    overall verdict-agreement, computed entirely from already-stored scores (no
    LLM call). Reports are append-only and versioned per
    ``(workspace_id, judge_config_key)`` — ``judge_config_key`` is the judge
    model's ``api_name`` — so re-running after a judge/rubric change keeps the old
    curves. The full report lives in ``metrics`` (per-dimension list, overall,
    recommendations); ``filters`` records the suite/template scope it was run over.
    """

    __tablename__ = "judge_calibrations"
    __table_args__ = (
        Index("idx_judge_calibrations_workspace", "workspace_id"),
        Index("idx_judge_calibrations_key", "workspace_id", "judge_config_key"),
        UniqueConstraint(
            "workspace_id",
            "judge_config_key",
            "version",
            name="uq_judge_calibration_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The judge identity this report calibrates — the judge model's api_name.
    judge_config_key: Mapped[str] = mapped_column(String(255), nullable=False)
    judge_model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # 1-based, increments per (workspace_id, judge_config_key).
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # Number of judge/human dimension pairs and distinct dimensions used.
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    n_dimensions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # {suite, template_id} scope the report was computed over.
    filters: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    # Full report: dimensions[], overall, recommendations, sample_size, ...
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # The acceptability cut applied to overall verdict-agreement kappa.
    threshold_kappa: Mapped[float] = mapped_column(Float, nullable=False)
    passed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    created_by: Mapped[str] = mapped_column(
        String(50), default="user", server_default="user"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
