import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"
    __table_args__ = (
        Index("idx_scheduled_jobs_enabled", "enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    cron_expr: Mapped[str | None] = mapped_column(String(200), nullable=True)
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fire_at: Mapped[datetime | None] = mapped_column(nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    last_fired_at: Mapped[datetime | None] = mapped_column(nullable=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
