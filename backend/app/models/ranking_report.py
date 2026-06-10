import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class RankingReport(Base):
    """A versioned pairwise-aggregation leaderboard — Aggregation Engine (E-19).

    Each row is one ranking of models or templates computed from a set of
    head-to-head matches by the pure :mod:`app.quality.aggregation` engine
    (Bradley-Terry or Elo) with bootstrap confidence intervals. Until the pairwise
    framework (E-21) exists the matches are *derived* from the pointwise
    ``quality_profile.weighted_score`` (same benchmark case, higher score wins);
    callers may also rank an explicit match list. Whichever the source, the full
    leaderboard — sorted players with rating + CI + win/loss/tie tallies — lives in
    the ``metrics`` JSONB.

    Reports are append-only and versioned per ``(workspace_id, ranking_key)`` where
    ``ranking_key`` is ``"{subject}:{method}"`` (e.g. ``model:bt``), so each axis ×
    method keeps its own history line. ``filters`` records the ``suite`` scope and
    the match ``source`` (derived vs explicit). Mirrors ``judge_calibrations`` /
    ``bias_reports``.
    """

    __tablename__ = "ranking_reports"
    __table_args__ = (
        Index("idx_ranking_reports_workspace", "workspace_id"),
        Index("idx_ranking_reports_key", "workspace_id", "ranking_key"),
        UniqueConstraint(
            "workspace_id",
            "ranking_key",
            "version",
            name="uq_ranking_report_version",
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
    # "{subject}:{method}" — the leaderboard identity this report belongs to.
    ranking_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # The ranked axis ("model" | "template") and rating method ("bt" | "elo").
    subject: Mapped[str] = mapped_column(String(50), nullable=False)
    method: Mapped[str] = mapped_column(String(50), nullable=False)
    # 1-based, increments per (workspace_id, ranking_key).
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    # Ranked players and aggregated matches in this report.
    n_players: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    n_matches: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # {suite, source} the report was computed over.
    filters: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    # Full leaderboard: status, players[] (rating, ci, w/l/t), params, derivation.
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # True when a real leaderboard was produced (status == "ok").
    passed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    created_by: Mapped[str] = mapped_column(
        String(50), default="user", server_default="user"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
