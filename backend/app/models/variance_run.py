import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class VarianceRun(Base):
    """Variance / Robustness Harness run (E-11, type R1).

    One row groups N re-runs of a single scenario. The scenario is either an
    existing finished task (``source_task_id`` — replayed N times) or a fresh
    spec (``source_spec`` = {title, description, reference_answer?}). Children
    are plain Tasks linked back via ``tasks.replay_of_task_id`` and listed in
    ``child_task_ids``; the orchestrator loop drains them under
    ``max_concurrent_agents``. The ``aggregate`` slot holds the computed
    distribution (outcome-score / trajectory-length / success-rate /
    tool-selection stability) once all children are terminal.
    """

    __tablename__ = "variance_runs"
    __table_args__ = (
        Index("idx_variance_runs_workspace", "workspace_id"),
        Index("idx_variance_runs_source", "source_task_id"),
        Index("idx_variance_runs_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Replay an existing finished task N times…
    source_task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    # …or run a fresh spec N times: {title, description, reference_answer?}.
    source_spec: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Pinned template for the children (denormalized from the source / request);
    # None => children go through normal orchestrator selection each run.
    template_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    n: Mapped[int] = mapped_column(Integer, nullable=False)
    parallel: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    cost_cap_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)

    # pending | running | done | capped | failed
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    child_task_ids: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    aggregate: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    accumulated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal("0"), server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
