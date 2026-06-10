import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import ForeignKey, Index, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PairwiseComparison(Base):
    """Pairwise Comparison Framework (E-21).

    One row is a head-to-head "which is better, A or B?" between two task
    results on a chosen ``subject`` axis (model / template / prompt). Pairwise
    judging is more reliable and human-natural than pointwise scoring, which
    clusters everything into 7-8 (§7.2). The verdict can come from an **LLM
    judge** (with position-bias mitigation — the same pair judged in both orders,
    agree → winner, disagree → tie) or from a **human**; both verdicts live on
    this row so judge↔human agreement (E-17) is row-local.

    Candidate B is either an existing finished task (``task_b_id`` set up front,
    ``status="ready"``) or **generated** on the fly by re-running ``source_task_id``
    with ``b_run_config`` overrides (``status="generating"`` until the scheduler
    tick clones B via ``clone_task_for_rerun`` and links it through
    ``tasks.replay_of_task_id``). Judged comparisons feed real matches to the E-19
    ranking engine → an **ELO leaderboard** (the E-19 → E-21 hand-off), shown in
    the existing Leaderboard tab.
    """

    __tablename__ = "pairwise_comparisons"
    __table_args__ = (
        Index("idx_pairwise_comparisons_workspace", "workspace_id"),
        Index("idx_pairwise_comparisons_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )

    # The leaderboard axis the two candidates compete on.
    subject: Mapped[str] = mapped_column(
        String(20), nullable=False, default="model", server_default="model"
    )

    # Generated mode: re-run this source task into candidate B with overrides.
    source_task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    # The two competitors. B is null until generated.
    task_a_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    task_b_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    # The rerun override blob for generated B ({model_id?|template_id?|soul_md?|…}).
    b_run_config: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # The identity of each side on the ``subject`` axis (the leaderboard player).
    player_a: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    player_b: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # pending | generating | ready | judged | failed
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    # llm | human — how this comparison is meant to be decided.
    judge_mode: Mapped[str] = mapped_column(
        String(10), nullable=False, default="llm", server_default="llm"
    )

    # Verdicts: "a" | "b" | "tie". Both may be present (judge + human → agreement).
    judge_verdict: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    human_verdict: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    # Per-order verdicts, position_bias_detected, judge model, reasoning, tokens.
    judge_detail: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    human_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    human_reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal("0"), server_default="0"
    )
    created_by: Mapped[str] = mapped_column(
        String(255), default="user", server_default="user"
    )

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
