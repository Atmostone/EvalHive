"""Immutable native inputs for one matrix configuration and dataset case."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BenchmarkExperimentPlan(Base):
    __tablename__ = "benchmark_experiment_plans"
    __table_args__ = (
        CheckConstraint(
            "condition_digest ~ '^[a-f0-9]{64}$'", name="ck_benchmark_experiment_plan_digest"
        ),
        ForeignKeyConstraint(
            ["experiment_id", "workspace_id"],
            ["experiments.id", "experiments.workspace_id"],
            ondelete="CASCADE",
            name="fk_benchmark_experiment_plan_scope",
        ),
        UniqueConstraint(
            "experiment_id", "config_key", "case_key", name="uq_benchmark_experiment_plan_cell"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    experiment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    config_key: Mapped[str] = mapped_column(String(64))
    case_key: Mapped[str] = mapped_column(String(128))
    condition_digest: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    private_inputs: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
