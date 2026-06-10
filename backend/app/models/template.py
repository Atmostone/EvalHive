import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, String, Text, Integer, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    soul_md: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_models.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Quality rubric used to score this template's task results (E-02). Optional;
    # falls back to a tag/default rubric when unset (see app.quality.rubric).
    rubric_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("rubrics.id", ondelete="SET NULL"),
        nullable=True,
    )
    # References into the workspace Tool & MCP Registry (SPA-41) — registry entry
    # ids (uuid strings). Replaces the old inline tools/mcp_servers; the spawn-time
    # resolver materializes these (plus any task-level override) into the tool name
    # list + MCP server dicts the agent container consumes.
    tool_ids: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    max_ram: Mapped[str] = mapped_column(String(20), default="2g", server_default="2g")
    max_cpu: Mapped[int] = mapped_column(Integer, default=100000, server_default="100000")
    timeout_minutes: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    tags: Mapped[list] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
