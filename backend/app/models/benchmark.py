"""Workspace configuration and immutable installed benchmark dataset revisions."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BenchmarkProfile(Base):
    __tablename__ = "benchmark_profiles"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_benchmark_profile_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    connector_id: Mapped[str] = mapped_column(String(200))
    connector_version: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    secret_refs: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    model_bindings: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class BenchmarkSecret(Base):
    __tablename__ = "benchmark_secrets"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_benchmark_secret_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    value: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class BenchmarkDatasetRevision(Base):
    __tablename__ = "benchmark_dataset_revisions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "connector_id", "connector_version", "dataset_key",
                         name="uq_benchmark_dataset_revision"),
        CheckConstraint("case_count >= 0", name="ck_benchmark_dataset_case_count"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    connector_id: Mapped[str] = mapped_column(String(200))
    connector_version: Mapped[str] = mapped_column(String(200))
    dataset_id: Mapped[str] = mapped_column(String(200))
    revision: Mapped[str] = mapped_column(Text)
    dataset_key: Mapped[str] = mapped_column(String(64))
    source_digest: Mapped[str] = mapped_column(String(71))
    receipt_digest: Mapped[str] = mapped_column(String(71))
    worker_image: Mapped[str] = mapped_column(Text)
    manifest: Mapped[dict] = mapped_column(JSONB)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    case_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BenchmarkDatasetCase(Base):
    __tablename__ = "benchmark_dataset_cases"
    __table_args__ = (
        UniqueConstraint("dataset_revision_id", "case_key", name="uq_benchmark_dataset_case"),
        UniqueConstraint("dataset_revision_id", "position", name="uq_benchmark_dataset_position"),
        CheckConstraint("position >= 0", name="ck_benchmark_dataset_position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("benchmark_dataset_revisions.id", ondelete="CASCADE"), index=True)
    case_key: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer)
    descriptor: Mapped[dict] = mapped_column(JSONB)
