import enum
import uuid
from datetime import datetime
from typing import Optional

from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TaskStatus(str, enum.Enum):
    BACKLOG = "backlog"
    READY = "ready"
    DECOMPOSING = "decomposing"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    FAILED = "failed"


class TaskPriority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("idx_tasks_status", "status"),
        Index("idx_tasks_parent", "parent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default=TaskStatus.BACKLOG.value,
        server_default=TaskStatus.BACKLOG.value,
    )
    priority: Mapped[str] = mapped_column(
        String(20), default=TaskPriority.MEDIUM.value,
        server_default=TaskPriority.MEDIUM.value,
    )
    template_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("templates.id"), nullable=True
    )
    agent_container_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    result_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Optional gold answer for reference-based evaluation (E-03).
    reference_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Optional canonical (gold) trajectory for trajectory matching (E-09). A
    # sequence of tool names, or a {nodes, edges} DAG. Non-null => the task has
    # a single valid path and the deterministic matcher applies.
    canonical_trajectory: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    result_files: Mapped[dict] = mapped_column(JSONB, default=list, server_default="[]")
    token_usage: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_retries: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    user_feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    orchestrator_feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model_used: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    input_price_per_1m_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 6), nullable=True
    )
    output_price_per_1m_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 6), nullable=True
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal("0"), server_default="0"
    )
    depends_on: Mapped[list] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list, server_default="{}"
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    log_archive_s3_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
