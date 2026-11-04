import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentEvent(Base):
    __tablename__ = "agent_events"
    __table_args__ = (
        Index("idx_events_task", "task_id"),
        Index("idx_events_created", "created_at"),
        Index("idx_events_type", "event_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True
    )
    agent_container_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
