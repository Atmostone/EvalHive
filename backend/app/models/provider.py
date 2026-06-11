import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Provider(Base):
    __tablename__ = "providers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_providers_workspace_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    api_key: Mapped[str] = mapped_column(String(500), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(500), nullable=False)
    # Max concurrent backend LLM calls to this provider (NULL → unbounded).
    # Subscription plans often limit simultaneous requests, not tokens (SPA-47).
    max_concurrency: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


class LLMModel(Base):
    __tablename__ = "llm_models"
    __table_args__ = (
        UniqueConstraint("provider_id", "api_name", name="uq_llm_models_provider_api_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("providers.id", ondelete="CASCADE"),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    api_name: Mapped[str] = mapped_column(String(255), nullable=False)
    input_price_per_1m_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0"), server_default="0", nullable=False
    )
    output_price_per_1m_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0"), server_default="0", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
