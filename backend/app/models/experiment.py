import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
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
    # Toolathlon-style executable cases (gold.external_eval) pass through two
    # extra states the container lifecycle needs; plain cases never enter them
    # (PENDING → RUNNING → SUCCESS/FAILED is unchanged).
    PREPROCESSING = "preprocessing"  # seeding + preprocess container before the agent
    RUNNING = "running"
    EVALUATING = "evaluating"  # external eval container after the agent settles
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
    # SPA-69: number of isolated Toolathlon PG lanes to run in parallel. None/0 →
    # serial (one Toolathlon cell at a time — the safe default, since all cases share
    # one mock postgres). >1 lets the scheduler claim up to that many Toolathlon cells
    # at once, each pinned to its own ``toolathlon_pg_lane_<i>`` instance so their
    # preprocess re-seeding cannot clobber each other.
    n_toolathlon_lanes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # {trajectory: bool=true, failure_modes: bool=false} — E-02 always runs.
    eval_config: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    accumulated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal("0"), server_default="0"
    )
    # Cached assembled report (experiment_report.build_report output).
    report: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- SPA-84 immutability -----------------------------------------------------
    # Bumped by every mutation (retry / add-config / retire-config). A cached
    # report records the revision it was computed from, which turns "is this
    # report stale?" from a guess into an equality check.
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # Hash over the inputs a report depends on (configurations, case keys,
    # n_runs_per_cell, eval_config). Catches input that changed without going
    # through a mutation — a hand-edited row, a restored dump — where the
    # revision counter cannot.
    input_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

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
    # Denormalised from the task, like the scores above (SPA-87). Infrastructure
    # failure types are excluded from the report's aggregates, so a leaderboard
    # cannot show a provider outage as a weak model. NULL = not classified, and
    # counts. Vocabulary + the contaminating subset: app/utils/failures.py.
    failure_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # --- Toolathlon executable evaluation (gold.external_eval) ------------------
    # ``external_verdict`` is the executable checker's pass/fail, kept SEPARATE
    # from ``status``: a run can be status=success (the agent finished, eval ran)
    # with external_verdict=False (the checker failed it) — the crux of RQ2.
    # None = no executable verdict (plain case, or eval infra error). The same
    # ``launch_time`` is reused for preprocess + eval (date-relative checks);
    # the *_container_id columns let a later tick re-inspect a detached container.
    external_verdict: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    launch_time: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    preprocess_container_id: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    eval_container_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    preprocess_retried: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    preprocess_started_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    preprocess_log: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    eval_log: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # SPA-69: which isolated PG lane (0..n_toolathlon_lanes-1) this Toolathlon run is
    # pinned to while in flight — its preprocess/eval/agent all target
    # ``toolathlon_pg_lane_<lane_index>``. None for plain runs and once settled (the
    # lane is freed for the next claim).
    lane_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # --- SPA-84 immutability -----------------------------------------------------
    # Executions this cell has had, the current one included. 0 = never claimed.
    # The row itself holds attempt number ``attempt_count``; the ones it
    # superseded live in ``experiment_attempts``.
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Set when this cell's configuration is retired. Row and lineage are kept;
    # the default report selection skips it.
    retired_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    # What this run ACTUALLY ran under, resolved at claim time: the effective
    # model's api_name, the template's content hash and the agent image it will
    # use. The config-level pin records the intent at start; the runtime
    # re-resolves at spawn, so only this can prove that every cell of a
    # config_key ran under one condition. NULL = never claimed.
    condition_fingerprint: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    # The same, minus the case-derived tool set — comparable across every cell of
    # a configuration, which the full hash is not.
    core_condition_fingerprint: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class ExperimentAttempt(Base):
    """One superseded execution of a matrix cell (SPA-84).

    ``ExperimentRun`` carries the *current* state of a cell. Before a retry
    clears that row, its state is copied here, so a cell that took three tries
    yields three addressable observations instead of one survivor. This matters
    beyond bookkeeping: a run that hit the iteration cap reports ``failed`` yet
    still carries a real evaluation, and retrying used to destroy it.

    Scores are denormalized for the same reason they are on ``ExperimentRun`` —
    they must outlive the task they came from.
    """

    __tablename__ = "experiment_attempts"
    __table_args__ = (
        UniqueConstraint(
            "experiment_run_id", "attempt_index", name="uq_experiment_attempts_cell"
        ),
        Index("idx_experiment_attempts_run", "experiment_run_id"),
        Index("idx_experiment_attempts_task", "task_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    experiment_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("experiment_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 1-based, in execution order.
    attempt_index: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)

    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), default=Decimal("0"), server_default="0"
    )
    weighted_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trajectory_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    failure_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    external_verdict: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    launch_time: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    lane_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # What this attempt ran under. Kept so a disagreement between the attempts of
    # one cell stays visible after the cell is reset.
    condition_fingerprint: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    core_condition_fingerprint: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )

    # Why this attempt stopped being current: 'retry' | 'config_retired'.
    retired_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
