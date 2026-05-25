import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PerturbationRun(Base):
    """Adversarial / Perturbation Judge run (E-12, type R2).

    One row groups a robustness probe of a single finished scenario
    (``source_task_id``). It runs ``base_n`` clean re-runs of the original input
    plus ``variants_per_transform`` perturbed re-runs for each enabled transform
    (paraphrase / noise / reorder / inject), then compares the perturbed outcome
    profiles against the clean baseline to produce a per-transform and overall
    robustness score. The ``inject`` transform additionally carries a runtime
    ``tool_injection`` payload containing ``injection_canary``; if the canary
    surfaces in a child's output the agent followed the injection (safety fail).

    Children are plain Tasks linked back via ``tasks.replay_of_task_id``; the
    orchestrator loop drains them under ``max_concurrent_agents``. The
    ``aggregate`` slot holds the computed comparison once all children terminate.
    """

    __tablename__ = "perturbation_runs"
    __table_args__ = (
        Index("idx_perturbation_runs_workspace", "workspace_id"),
        Index("idx_perturbation_runs_source", "source_task_id"),
        Index("idx_perturbation_runs_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The finished scenario being probed (its input is perturbed; cleared on delete).
    source_task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    # Pinned template for every child (denormalized from the source / request);
    # None => children go through normal orchestrator selection each run.
    template_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Enabled transform keys (subset of paraphrase|noise|reorder|inject).
    transforms: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    variants_per_transform: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    base_n: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    parallel: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    cost_cap_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), nullable=True)
    # Unique marker the inject payload asks the agent to emit; its presence in a
    # child's output is the deterministic "agent followed the injection" signal.
    injection_canary: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # pending | running | done | capped | failed
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    # Clean baseline children (original input).
    base_task_ids: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    # Perturbed children grouped by transform: {transform_key: [task_id, ...]}.
    perturbed_task_ids: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}"
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
