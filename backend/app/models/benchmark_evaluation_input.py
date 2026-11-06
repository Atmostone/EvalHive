"""Durable ownership of local read-only checkpoint materializations."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BenchmarkEvaluationInput(Base):
    __tablename__ = "benchmark_evaluation_inputs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["control_id", "trial_id", "workspace_id"],
            [
                "benchmark_evaluation_controls.id",
                "benchmark_evaluation_controls.source_trial_id",
                "benchmark_evaluation_controls.workspace_id",
            ],
            ondelete="CASCADE",
            name="fk_evaluation_input_scope",
        ),
        UniqueConstraint("control_id", "artifact_id", name="uq_evaluation_input_artifact"),
        CheckConstraint(
            "origin_generation > 0 AND size_bytes >= 0", name="ck_evaluation_input_size"
        ),
        CheckConstraint(
            "state IN ('reserved','ready','released')", name="ck_evaluation_input_state"
        ),
        CheckConstraint(
            "(state = 'released') = (released_at IS NOT NULL)", name="ck_evaluation_input_released"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    control_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    trial_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    origin_generation: Mapped[int] = mapped_column(Integer)
    controller_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    artifact_id: Mapped[str] = mapped_column(String(200))
    reference_hash: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    private_input: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    state: Mapped[str] = mapped_column(String(20), server_default="reserved", default="reserved")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
