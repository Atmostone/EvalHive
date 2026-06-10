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


class BiasReport(Base):
    """A versioned bias-mitigation report — Bias Mitigation Toolkit (E-18).

    Each row is one controlled A/B re-judge of the calibration set: the LLM judge
    (E-02) is re-run over every task that carries human feedback (E-05) with the
    prompt-level mitigations OFF and then ON, and the two passes are compared for
    agreement-with-human (the E-17 statistics) plus per-bias diagnostics
    (verbosity, score-clustering, self-preference; position bias is deferred to
    pairwise / E-21). Unlike the E-17 calibration report this DOES make LLM calls.

    Reports are append-only and versioned per ``(workspace_id, judge_config_key)``
    — ``judge_config_key`` is the judge model's ``api_name`` — mirroring
    ``judge_calibrations``. The full before/after report lives in ``metrics``;
    ``filters`` records the suite/template scope.
    """

    __tablename__ = "bias_reports"
    __table_args__ = (
        Index("idx_bias_reports_workspace", "workspace_id"),
        Index("idx_bias_reports_key", "workspace_id", "judge_config_key"),
        UniqueConstraint(
            "workspace_id",
            "judge_config_key",
            "version",
            name="uq_bias_report_version",
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
    # The judge identity this report was run against — the judge model's api_name.
    judge_config_key: Mapped[str] = mapped_column(String(255), nullable=False)
    judge_model: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # 1-based, increments per (workspace_id, judge_config_key).
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # Number of re-judged judge/human dimension pairs and distinct dimensions.
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    n_dimensions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # {suite, template_id} scope the report was computed over.
    filters: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    # Full before/after report: before, after, dimensions_delta, overall_delta,
    # diagnostics, toggles_requested, ...
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # The acceptability cut applied to agreement kappa (shared with E-17).
    threshold_kappa: Mapped[float] = mapped_column(Float, nullable=False)
    # True when mitigation improved overall agreement-with-human.
    passed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    created_by: Mapped[str] = mapped_column(
        String(50), default="user", server_default="user"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
