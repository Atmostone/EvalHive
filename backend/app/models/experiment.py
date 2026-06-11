import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ExperimentStatus(str, enum.Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    # Budget limit reached: remaining cells skipped, partial results kept.
    CAPPED = "capped"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExperimentRunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class Experiment(Base):
    """A/B Matrix Harness experiment (SPA-40).

    First-class experiment object: a frozen dataset of cases × a matrix of
    agent configurations × ``n_runs_per_cell`` repetitions, executed over the
    benchmark execution path (direct agent spawn bypassing orchestrator
    decision-making and the approval/board flow) and evaluated unconditionally
    (E-02 outcome + E-07 trajectory, E-20 snapshot per run).

    ``dataset``/``matrix_spec`` keep the raw user request (clone fidelity);
    ``dataset_cases``/``configurations`` are the expanded, frozen forms the
    runner actually executes — immune to later edits of suites/templates.
    ``report`` caches the assembled report (heatmap / Pareto / leaderboard /
    significance / orchestrator on-off comparison) once runs settle.
    """

    __tablename__ = "experiments"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_experiments_workspace_name"),
        Index("idx_experiments_workspace", "workspace_id"),
        Index("idx_experiments_status", "status"),
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
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ExperimentStatus.DRAFT.value,
        server_default=ExperimentStatus.DRAFT.value,
    )

    # Raw dataset spec as requested: {source: benchmark_suite|tasks|upload, ...}.
    dataset: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Frozen cases: [{case_key, title, description, reference_answer?,
    # canonical_trajectory?, capability_spec?}]. The runner only reads these.
    dataset_cases: Mapped[list] = mapped_column(JSONB, nullable=False)
    # Raw matrix request: {configurations: [...], axes: {...}} (clone fidelity).
    matrix_spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Expanded configurations: [{config_key, label, fingerprint, orchestrator,
    # template_id?, model_id?, temperature?, seed?, soul_md?, tools_override?,
    # memory_mode?}].
    configurations: Mapped[list] = mapped_column(JSONB, nullable=False)

    n_runs_per_cell: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    budget_limit_usd: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 6), nullable=True
    )
    max_parallel: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # {trajectory: bool=true, failure_modes: bool=false} — E-02 always runs.
    eval_config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    accumulated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal("0"), server_default="0"
    )
    # Cached assembled report (experiment_report.build_report output).
    report: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_by: Mapped[str] = mapped_column(
        String(255), default="user", server_default="user"
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class ExperimentRun(Base):
    """One matrix cell execution: (config_key × case_key × run_index) → task.

    All rows are pre-created as ``pending`` when the experiment starts, which
    makes the scheduler tick trivially idempotent (claim a pending row and
    create its Task in one commit) and gives the progress matrix exact totals.
    Scores/cost/duration are denormalized from the task + quality record at
    settle time so results survive later task deletion (quality_records
    cascade away with tasks; these rows and the cached report do not).
    """

    __tablename__ = "experiment_runs"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "config_key",
            "case_key",
            "run_index",
            name="uq_experiment_runs_cell",
        ),
        Index("idx_experiment_runs_exp_status", "experiment_id", "status"),
        Index("idx_experiment_runs_task", "task_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    config_key: Mapped[str] = mapped_column(String(64), nullable=False)
    case_key: Mapped[str] = mapped_column(String(128), nullable=False)
    run_index: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ExperimentRunStatus.PENDING.value,
        server_default=ExperimentRunStatus.PENDING.value,
    )

    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal("0"), server_default="0"
    )
    weighted_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trajectory_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
