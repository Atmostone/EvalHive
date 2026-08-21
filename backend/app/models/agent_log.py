import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentLogChunk(Base):
    __tablename__ = "agent_log_chunks"
    __table_args__ = (
        UniqueConstraint("task_id", "chunk_seq", name="uq_agent_log_chunk_seq"),
        Index("idx_agent_log_chunks_task_seq", "task_id", "chunk_seq"),
        Index("idx_agent_log_chunks_workspace", "workspace_id"),
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
    chunk_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # SPA-86 — the call that produced `content`, not just its name. Without the
    # arguments the process judge's `parameter_quality` axis has no subject.
    arguments: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    arguments_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # A tool output over the transport cap is split across several rows; these
    # let the trace cleaner put them back together as one step instead of
    # reading one call as N.
    tool_call_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    part_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    part_total: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # SPA-114 — the model's private deliberation preceding this step. A reasoning
    # model returns it in a field separate from the answer, and nothing in this
    # repository read it: the platform stored the conclusion and discarded the
    # thinking that produced it, then asked a process judge how the agent worked.
    reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentLogDelivery(Base):
    __tablename__ = "agent_log_deliveries"
    __table_args__ = (
        UniqueConstraint("task_id", "idempotency_key", name="uq_agent_log_delivery"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
