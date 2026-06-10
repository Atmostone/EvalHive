import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RegistryEntry(Base):
    """User(workspace)-level Tool & MCP Registry entry (SPA-41).

    A single source of truth for a tool or MCP server, configured once and
    referenced by agent templates (``templates.tool_ids``) and experiment/task
    overrides instead of being duplicated inline on every template. ``kind``
    distinguishes a ``builtin`` capability (a tool name the agent enables, e.g.
    ``bash``) from an ``mcp`` server (``config`` carries the non-secret
    ``{command, args, url?}``; ``secrets`` carries the credential env map).

    Secrets are stored plain text and masked in API responses (mirroring
    ``Provider.api_key``); only the spawn-time resolver reveals them into the
    agent container env. The ``secrets`` indirection is the seam for a future
    S-06 Vault/encryption follow-up.
    """

    __tablename__ = "registry_entries"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_registry_entries_workspace_name"),
        Index("idx_registry_entries_workspace", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # builtin | mcp
    kind: Mapped[str] = mapped_column(
        String(10), nullable=False, default="builtin", server_default="builtin"
    )
    # Non-secret config. builtin: arbitrary (e.g. {}); mcp: {command, args:[], url?}.
    config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    # Credential env map {ENV_KEY: value}; plain text, masked on read.
    secrets: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(
        String(255), default="user", server_default="user"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
