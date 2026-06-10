"""Quality Rubric Engine (E-02) API.

Workspace-scoped CRUD for rubrics, plus reading a task's quality profile and
triggering an on-demand evaluation. Auto-evaluation otherwise runs as the
`quality_judge_evaluate` scheduler job (gated by the `quality_eval_enabled`
setting).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, get_current_workspace, require_role
from app.database import get_db
from app.models.quality_record import QualityRecord
from app.models.rubric import Rubric
from app.models.task import Task
from app.models.variance_run import VarianceRun
from app.models.perturbation_run import PerturbationRun
from app.models.user import User
from app.models.workspace import Workspace
from app.quality.trace_cleaner import (
    DEFAULT_TOOL_OUTPUT_TOKEN_CAP,
    TOKEN_CAP_MAX,
    TOKEN_CAP_MIN,
)

router = APIRouter(prefix="/api/quality", tags=["quality"])

EvaluatorType = Literal["judge", "objective", "human", "reference"]
ReferenceMode = Literal["pointwise", "exact", "fuzzy", "semantic"]
ProbeType = Literal["lint", "types"]


class DimensionBody(BaseModel):
    key: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    evaluator: EvaluatorType = "judge"
    # Only meaningful when evaluator == "reference" (E-03); cleared otherwise.
    reference_mode: Optional[ReferenceMode] = None
    # Only meaningful when evaluator == "objective" (E-04); cleared otherwise.
    probe: Optional[ProbeType] = None
    weight: float = Field(default=1.0, ge=0)
    threshold: Optional[int] = Field(default=None, ge=0, le=10)
    critical: bool = False

    @model_validator(mode="after")
    def _evaluator_fields_consistency(self) -> "DimensionBody":
        if self.evaluator == "reference":
            if self.reference_mode is None:
                self.reference_mode = "pointwise"
        else:
            self.reference_mode = None
        if self.evaluator == "objective":
            if self.probe is None:
                self.probe = "lint"
        else:
            self.probe = None
        return self


class RubricCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    applies_to: Optional[str] = Field(default=None, max_length=50)
    is_default: bool = False
    dimensions: list[DimensionBody] = Field(default_factory=list)


class RubricUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    applies_to: Optional[str] = Field(default=None, max_length=50)
    is_default: Optional[bool] = None
    dimensions: Optional[list[DimensionBody]] = None


class FeedbackDimensionBody(BaseModel):
    """One human rating, mirroring a quality-profile dimension (E-05)."""

    key: str = Field(min_length=1, max_length=100)
    name: Optional[str] = Field(default=None, max_length=200)
    score: int = Field(ge=0, le=10)
    comment: Optional[str] = None


class HumanFeedbackBody(BaseModel):
    verdict: Optional[Literal["approve", "reject"]] = None
    overall_comment: Optional[str] = None
    dimensions: list[FeedbackDimensionBody] = Field(default_factory=list)


def _rubric_to_dict(r: Rubric) -> dict:
    return {
        "id": str(r.id),
        "workspace_id": str(r.workspace_id),
        "name": r.name,
        "description": r.description,
        "applies_to": r.applies_to,
        "is_default": r.is_default,
        "dimensions": list(r.dimensions or []),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


async def _get_owned_rubric(db: AsyncSession, rubric_id: str, workspace: Workspace) -> Rubric:
    try:
        rid = uuid.UUID(rubric_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid rubric id")
    rubric = await db.get(Rubric, rid)
    if rubric is None or rubric.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="rubric not found")
    return rubric


async def _clear_other_defaults(db: AsyncSession, workspace_id, keep_id=None):
    stmt = update(Rubric).where(
        Rubric.workspace_id == workspace_id, Rubric.is_default.is_(True)
    )
    if keep_id is not None:
        stmt = stmt.where(Rubric.id != keep_id)
    await db.execute(stmt.values(is_default=False))


@router.get("/rubrics")
async def list_rubrics(
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(Rubric)
            .where(Rubric.workspace_id == workspace.id)
            .order_by(Rubric.created_at)
        )
    ).scalars().all()
    return [_rubric_to_dict(r) for r in rows]


@router.post("/rubrics")
async def create_rubric(
    body: RubricCreate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    rubric = Rubric(
        workspace_id=workspace.id,
        name=body.name,
        description=body.description,
        applies_to=body.applies_to,
        is_default=body.is_default,
        dimensions=[d.model_dump() for d in body.dimensions],
    )
    if body.is_default:
        await _clear_other_defaults(db, workspace.id)
    db.add(rubric)
    await db.commit()
    await db.refresh(rubric)
    return _rubric_to_dict(rubric)


@router.get("/rubrics/{rubric_id}")
async def get_rubric(
    rubric_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    return _rubric_to_dict(await _get_owned_rubric(db, rubric_id, workspace))


@router.patch("/rubrics/{rubric_id}")
async def update_rubric(
    rubric_id: str,
    body: RubricUpdate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    rubric = await _get_owned_rubric(db, rubric_id, workspace)
    fields = body.model_dump(exclude_unset=True)
    if "dimensions" in fields and fields["dimensions"] is not None:
        fields["dimensions"] = [
            d if isinstance(d, dict) else d.model_dump() for d in body.dimensions
        ]
    if fields.get("is_default"):
        await _clear_other_defaults(db, workspace.id, keep_id=rubric.id)
    for key, value in fields.items():
        setattr(rubric, key, value)
    await db.commit()
    await db.refresh(rubric)
    return _rubric_to_dict(rubric)


@router.delete("/rubrics/{rubric_id}")
async def delete_rubric(
    rubric_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    rubric = await _get_owned_rubric(db, rubric_id, workspace)
    await db.delete(rubric)
    await db.commit()
    return {"ok": True}


async def _get_owned_task(db: AsyncSession, task_id: str, workspace: Workspace) -> Task:
    try:
        tid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid task id")
    task = await db.get(Task, tid)
    if task is None or task.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.get("/records/{task_id}/profile")
async def get_profile(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    rec = (
        await db.execute(
            select(QualityRecord).where(
                QualityRecord.task_id == uuid.UUID(task_id),
                QualityRecord.workspace_id == workspace.id,
            )
        )
    ).scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=404, detail="quality record not found")
    return {"task_id": task_id, "quality_profile": rec.quality_profile}


@router.post("/records/{task_id}/evaluate")
async def evaluate_record(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """On-demand evaluation. Skipped (profile null) if no rubric or judge model."""
    from app.quality.judge import evaluate_task_quality

    task = await _get_owned_task(db, task_id, workspace)
    profile = await evaluate_task_quality(db, task)
    if profile is None:
        return {
            "task_id": task_id,
            "quality_profile": None,
            "skipped": True,
            "detail": "no rubric matched, or no quality-judge/orchestrator model configured",
        }
    return {"task_id": task_id, "quality_profile": profile, "skipped": False}


@router.get("/records/{task_id}/trajectory")
async def get_trajectory(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Read the 6-axis trajectory profile (E-07), or null if not yet judged."""
    rec = (
        await db.execute(
            select(QualityRecord).where(
                QualityRecord.task_id == uuid.UUID(task_id),
                QualityRecord.workspace_id == workspace.id,
            )
        )
    ).scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=404, detail="quality record not found")
    return {"task_id": task_id, "trajectory_profile": rec.trajectory_profile}


@router.post("/records/{task_id}/evaluate-trajectory")
async def evaluate_trajectory_record(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """On-demand trajectory evaluation (E-07). Skipped (profile null) when there
    is no judge model or the cleaned trace has no steps."""
    from app.quality.trajectory import evaluate_task_trajectory

    task = await _get_owned_task(db, task_id, workspace)
    profile = await evaluate_task_trajectory(db, task)
    if profile is None:
        return {
            "task_id": task_id,
            "trajectory_profile": None,
            "skipped": True,
            "detail": "empty trajectory, or no quality-judge/orchestrator model configured",
        }
    return {"task_id": task_id, "trajectory_profile": profile, "skipped": False}


@router.get("/records/{task_id}/trajectory-evidence")
async def get_trajectory_evidence(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Read the TRACE evidence-bank profile (E-08), or null if not yet judged."""
    rec = (
        await db.execute(
            select(QualityRecord).where(
                QualityRecord.task_id == uuid.UUID(task_id),
                QualityRecord.workspace_id == workspace.id,
            )
        )
    ).scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=404, detail="quality record not found")
    return {"task_id": task_id, "trajectory_evidence_profile": rec.trajectory_evidence_profile}


@router.post("/records/{task_id}/evaluate-trajectory-evidence")
async def evaluate_trajectory_evidence_record(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """On-demand TRACE evidence-bank evaluation (E-08). Skipped (profile null) when
    there is no judge model or the cleaned trace has no steps."""
    from app.quality.trace_evidence import evaluate_task_trace_evidence

    task = await _get_owned_task(db, task_id, workspace)
    profile = await evaluate_task_trace_evidence(db, task)
    if profile is None:
        return {
            "task_id": task_id,
            "trajectory_evidence_profile": None,
            "skipped": True,
            "detail": "empty trajectory, or no quality-judge/orchestrator model configured",
        }
    return {"task_id": task_id, "trajectory_evidence_profile": profile, "skipped": False}


@router.get("/records/{task_id}/trajectory-match")
async def get_trajectory_match(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Read the deterministic trajectory-match profile (E-09), or null if absent."""
    rec = (
        await db.execute(
            select(QualityRecord).where(
                QualityRecord.task_id == uuid.UUID(task_id),
                QualityRecord.workspace_id == workspace.id,
            )
        )
    ).scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=404, detail="quality record not found")
    return {"task_id": task_id, "trajectory_match_profile": rec.trajectory_match_profile}


@router.post("/records/{task_id}/evaluate-trajectory-match")
async def evaluate_trajectory_match_record(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """On-demand trajectory matching (E-09). Skipped (profile null) when the task
    has no canonical_trajectory set."""
    from app.quality.trajectory_match import evaluate_task_trajectory_match

    task = await _get_owned_task(db, task_id, workspace)
    profile = await evaluate_task_trajectory_match(db, task)
    if profile is None:
        return {
            "task_id": task_id,
            "trajectory_match_profile": None,
            "skipped": True,
            "detail": "task has no canonical_trajectory to match against",
        }
    return {"task_id": task_id, "trajectory_match_profile": profile, "skipped": False}


@router.get("/records/{task_id}/capability")
async def get_capability(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Read the deterministic capability-isolation profile (E-13), or null if absent."""
    rec = (
        await db.execute(
            select(QualityRecord).where(
                QualityRecord.task_id == uuid.UUID(task_id),
                QualityRecord.workspace_id == workspace.id,
            )
        )
    ).scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=404, detail="quality record not found")
    return {"task_id": task_id, "capability_profile": rec.capability_profile}


@router.post("/records/{task_id}/evaluate-capability")
async def evaluate_capability_record(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """On-demand capability-isolation evaluation (E-13). Skipped (profile null) when
    the task has no capability_spec set."""
    from app.quality.capability import evaluate_task_capability

    task = await _get_owned_task(db, task_id, workspace)
    profile = await evaluate_task_capability(db, task)
    if profile is None:
        return {
            "task_id": task_id,
            "capability_profile": None,
            "skipped": True,
            "detail": "task has no capability_spec (required_tools) to check against",
        }
    return {"task_id": task_id, "capability_profile": profile, "skipped": False}


@router.get("/capability/aggregate")
async def capability_aggregate(
    category: Optional[str] = Query(None),
    model_used: Optional[str] = Query(None),
    template_id: Optional[str] = Query(None),
    suite: Optional[str] = Query(None),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate capability profiles (E-13) into capability_score(s) across the
    workspace, with breakdowns by category / model / template — the model
    breakdown is the "compare models by capability" view. `suite` restricts to one
    Benchmark Case Store suite."""
    from app.quality.capability import aggregate_capability

    return await aggregate_capability(
        db,
        workspace_id=workspace.id,
        category=category,
        model_used=model_used,
        template_id=_parse_uuid(template_id, "template_id"),
        suite=suite,
    )


@router.get("/records/{task_id}/failure-modes")
async def get_failure_modes(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Read the failure-mode classification profile (E-14), or null if absent."""
    rec = (
        await db.execute(
            select(QualityRecord).where(
                QualityRecord.task_id == uuid.UUID(task_id),
                QualityRecord.workspace_id == workspace.id,
            )
        )
    ).scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=404, detail="quality record not found")
    return {"task_id": task_id, "failure_profile": rec.failure_profile}


@router.post("/records/{task_id}/evaluate-failure-modes")
async def evaluate_failure_modes_record(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """On-demand failure-mode classification (E-14). Skipped (profile null) when
    there is no judge model or the task has an empty trace."""
    from app.quality.failure_modes import evaluate_task_failure_modes

    task = await _get_owned_task(db, task_id, workspace)
    profile = await evaluate_task_failure_modes(db, task)
    if profile is None:
        return {
            "task_id": task_id,
            "failure_profile": None,
            "skipped": True,
            "detail": "no judge/orchestrator model configured, or the task has no trace",
        }
    return {"task_id": task_id, "failure_profile": profile, "skipped": False}


@router.get("/failure-modes/aggregate")
async def failure_modes_aggregate(
    model_used: Optional[str] = Query(None),
    template_id: Optional[str] = Query(None),
    failure_class: Optional[str] = Query(None),
    suite: Optional[str] = Query(None),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate failure profiles (E-14) into per-class distributions across the
    workspace, with breakdowns by class / model / template — the distribution of
    failure types per (model, template). `failure_class` narrows to runs carrying
    that class; `suite` restricts to one Benchmark Case Store suite."""
    from app.quality.failure_modes import aggregate_failure_modes

    return await aggregate_failure_modes(
        db,
        workspace_id=workspace.id,
        model_used=model_used,
        template_id=_parse_uuid(template_id, "template_id"),
        failure_class=failure_class,
        suite=suite,
    )


@router.get("/records/{task_id}/hallucinations")
async def get_hallucinations(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Read the hallucination fact-check profile (E-15), or null if absent."""
    rec = (
        await db.execute(
            select(QualityRecord).where(
                QualityRecord.task_id == uuid.UUID(task_id),
                QualityRecord.workspace_id == workspace.id,
            )
        )
    ).scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=404, detail="quality record not found")
    return {"task_id": task_id, "hallucination_profile": rec.hallucination_profile}


@router.post("/records/{task_id}/evaluate-hallucinations")
async def evaluate_hallucinations_record(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """On-demand hallucination fact-check (E-15). Skipped (profile null) when
    there is no judge model, no result deliverable, or an empty trace."""
    from app.quality.hallucination import evaluate_task_hallucinations

    task = await _get_owned_task(db, task_id, workspace)
    profile = await evaluate_task_hallucinations(db, task)
    if profile is None:
        return {
            "task_id": task_id,
            "hallucination_profile": None,
            "skipped": True,
            "detail": "no judge/orchestrator model, no result deliverable, or no trace",
        }
    return {"task_id": task_id, "hallucination_profile": profile, "skipped": False}


@router.get("/hallucinations/aggregate")
async def hallucinations_aggregate(
    model_used: Optional[str] = Query(None),
    template_id: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    suite: Optional[str] = Query(None),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate hallucination profiles (E-15) across the workspace, with
    per-category checked/hallucinated counts and breakdowns by category / model /
    template — the hallucination rate per (model, template). `category` narrows to
    runs with ≥1 hallucination in that category; `suite` restricts to one
    Benchmark Case Store suite."""
    from app.quality.hallucination import aggregate_hallucinations

    return await aggregate_hallucinations(
        db,
        workspace_id=workspace.id,
        model_used=model_used,
        template_id=_parse_uuid(template_id, "template_id"),
        category=category,
        suite=suite,
    )


@router.get("/records/{task_id}/calibration")
async def get_calibration(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Read the confidence-calibration profile (E-16), or null if absent."""
    rec = (
        await db.execute(
            select(QualityRecord).where(
                QualityRecord.task_id == uuid.UUID(task_id),
                QualityRecord.workspace_id == workspace.id,
            )
        )
    ).scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=404, detail="quality record not found")
    return {"task_id": task_id, "calibration_profile": rec.calibration_profile}


@router.post("/records/{task_id}/evaluate-calibration")
async def evaluate_calibration_record(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """On-demand confidence-calibration probe (E-16). Skipped (profile null) when
    no model is resolvable, there is no result deliverable, or the E-02 profile
    has no correctness signal to calibrate against."""
    from app.quality.calibration import evaluate_task_calibration

    task = await _get_owned_task(db, task_id, workspace)
    profile = await evaluate_task_calibration(db, task)
    if profile is None:
        return {
            "task_id": task_id,
            "calibration_profile": None,
            "skipped": True,
            "detail": "no model resolvable, no result deliverable, or no correctness signal",
        }
    return {"task_id": task_id, "calibration_profile": profile, "skipped": False}


@router.get("/calibration/aggregate")
async def calibration_aggregate(
    model_used: Optional[str] = Query(None),
    template_id: Optional[str] = Query(None),
    suite: Optional[str] = Query(None),
    bins: int = Query(10, ge=2, le=20),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate calibration profiles (E-16) across the workspace into ECE / Brier
    / reliability-diagram metrics, overall and broken down by model / template,
    with a per-model recommendation. `suite` restricts to one Benchmark Case Store
    suite; `bins` controls the reliability-diagram resolution."""
    from app.quality.calibration import aggregate_calibration

    return await aggregate_calibration(
        db,
        workspace_id=workspace.id,
        model_used=model_used,
        template_id=_parse_uuid(template_id, "template_id"),
        suite=suite,
        bins=bins,
    )


@router.get("/records/{task_id}/feedback")
async def get_feedback(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Human feedback (E-05) for a task, or null if none submitted yet."""
    from app.quality.feedback import get_human_feedback

    task = await _get_owned_task(db, task_id, workspace)
    return {"task_id": task_id, "human_feedback": await get_human_feedback(db, task)}


@router.put("/records/{task_id}/feedback")
async def put_feedback(
    task_id: str,
    body: HumanFeedbackBody,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Upsert structured human feedback (E-05). Stored alongside the judge profile
    in the task's quality record; a parallel signal that does not change the gate."""
    from app.quality.feedback import save_human_feedback

    task = await _get_owned_task(db, task_id, workspace)
    feedback = await save_human_feedback(db, task, body.model_dump(), user.email)
    return {"task_id": task_id, "human_feedback": feedback}


@router.get("/records/{task_id}/trace")
async def get_cleaned_trace(
    task_id: str,
    tool_output_token_cap: int = Query(
        DEFAULT_TOOL_OUTPUT_TOKEN_CAP, ge=TOKEN_CAP_MIN, le=TOKEN_CAP_MAX
    ),
    keep_tail_on_error: bool = False,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Cleaned, judge-ready trajectory (E-06): the input the trajectory judge
    (E-07) will consume. Drops the system snapshot and noise events, truncates
    long tool outputs, reports token savings. Read-only; computed on demand,
    not persisted."""
    from app.quality.trace_cleaner import TraceCleanerConfig, build_cleaned_trace

    task = await _get_owned_task(db, task_id, workspace)
    config = TraceCleanerConfig(
        tool_output_token_cap=tool_output_token_cap,
        keep_tail_on_error=keep_tail_on_error,
    )
    trace = await build_cleaned_trace(db, task, config=config)
    return {"task_id": task_id, "cleaned_trace": trace}


@router.get("/calibration")
async def calibration_export(
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Flattened judge-vs-human pairs for calibration (E-17 input). One row per
    rated dimension across all records that carry human feedback. Shares its
    row-building with the E-17 report via ``collect_judge_human_pairs``."""
    from app.quality.judge_calibration import collect_judge_human_pairs

    return await collect_judge_human_pairs(db, workspace.id)


# --------------------------------------------------------------------------- #
# Judge Calibration Protocol (E-17)
# --------------------------------------------------------------------------- #
class JudgeCalibrationRunBody(BaseModel):
    suite: Optional[str] = None
    template_id: Optional[str] = None


@router.post("/judge-calibration/run")
async def run_judge_calibration_endpoint(
    body: JudgeCalibrationRunBody | None = None,
    workspace: Workspace = Depends(get_current_workspace),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Validate the LLM judge (E-02) against human feedback (E-05) over stored
    scores and persist the next versioned report (E-17). No LLM call."""
    from app.quality.judge_calibration import run_judge_calibration

    body = body or JudgeCalibrationRunBody()
    return await run_judge_calibration(
        db,
        workspace_id=workspace.id,
        suite=body.suite,
        template_id=_parse_uuid(body.template_id, "template_id"),
        created_by=getattr(user, "email", None) or "user",
    )


@router.get("/judge-calibration")
async def get_judge_calibration_endpoint(
    judge_config_key: Optional[str] = Query(None),
    history: bool = Query(False),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Latest judge-calibration report; with ``history=true`` also returns the
    version history (newest first)."""
    from app.quality.judge_calibration import (
        get_judge_calibration,
        list_judge_calibrations,
    )

    latest = await get_judge_calibration(
        db, workspace_id=workspace.id, judge_config_key=judge_config_key
    )
    if not history:
        return latest
    versions = await list_judge_calibrations(
        db, workspace_id=workspace.id, judge_config_key=judge_config_key
    )
    return {"latest": latest, "history": versions}


@router.get("/judge-calibration/badge")
async def judge_calibration_badge(
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Compact badge: 'judge calibrated against N humans, kappa=X.X'."""
    from app.quality.judge_calibration import get_judge_calibration_badge

    return await get_judge_calibration_badge(db, workspace_id=workspace.id)


# --------------------------------------------------------------------------- #
# Bias Mitigation Toolkit (E-18)
# --------------------------------------------------------------------------- #
class BiasReportRunBody(BaseModel):
    suite: Optional[str] = None
    template_id: Optional[str] = None
    # Per-toggle overrides for the "after" pass. When omitted, the saved
    # bias_mitigation_* settings are used (and a full A/B is run if none are on).
    verbosity: Optional[bool] = None
    score_clustering: Optional[bool] = None
    self_preference: Optional[bool] = None
    position: Optional[bool] = None


@router.post("/bias-report/run")
async def run_bias_report_endpoint(
    body: BiasReportRunBody | None = None,
    workspace: Workspace = Depends(get_current_workspace),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Controlled A/B re-judge of the calibration set with mitigations OFF vs ON
    (E-18) and persist the next versioned before/after report. Spends LLM calls."""
    from app.quality.bias_mitigation import run_bias_report

    body = body or BiasReportRunBody()
    overrides = {
        k: v
        for k, v in {
            "verbosity": body.verbosity,
            "score_clustering": body.score_clustering,
            "self_preference": body.self_preference,
            "position": body.position,
        }.items()
        if v is not None
    }
    return await run_bias_report(
        db,
        workspace_id=workspace.id,
        suite=body.suite,
        template_id=_parse_uuid(body.template_id, "template_id"),
        toggles=overrides or None,
        created_by=getattr(user, "email", None) or "user",
    )


@router.get("/bias-report")
async def get_bias_report_endpoint(
    judge_config_key: Optional[str] = Query(None),
    history: bool = Query(False),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Latest bias report; with ``history=true`` also returns the version history
    (newest first)."""
    from app.quality.bias_mitigation import get_bias_report, list_bias_reports

    latest = await get_bias_report(
        db, workspace_id=workspace.id, judge_config_key=judge_config_key
    )
    if not history:
        return latest
    versions = await list_bias_reports(
        db, workspace_id=workspace.id, judge_config_key=judge_config_key
    )
    return {"latest": latest, "history": versions}


# --------------------------------------------------------------------------- #
# Aggregation Engine — Bradley-Terry / Elo leaderboard (E-19)
# --------------------------------------------------------------------------- #
class MatchBody(BaseModel):
    player_a: str = Field(min_length=1)
    player_b: str = Field(min_length=1)
    outcome: Literal["a", "b", "tie"]
    weight: int = Field(default=1, ge=1)


class RankingRunBody(BaseModel):
    # Which axis to rank and how to aggregate.
    subject: Literal["model", "template"] = "model"
    method: Literal["bt", "elo"] = "bt"
    suite: Optional[str] = None
    # Explicit matches bypass the pointwise-score derivation (the literal
    # rank(pairwise_results) API); omit to derive matches from stored scores.
    matches: Optional[list[MatchBody]] = None


@router.post("/ranking/run")
async def run_ranking_endpoint(
    body: RankingRunBody | None = None,
    workspace: Workspace = Depends(get_current_workspace),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Rank models/templates from pairwise matches via Bradley-Terry or Elo and
    persist the next versioned leaderboard (E-19). Matches are derived from stored
    pointwise scores unless an explicit ``matches`` list is supplied. No LLM call."""
    from app.quality.ranking import run_ranking

    body = body or RankingRunBody()
    matches = [m.model_dump() for m in body.matches] if body.matches is not None else None
    return await run_ranking(
        db,
        workspace_id=workspace.id,
        subject=body.subject,
        method=body.method,
        suite=body.suite,
        matches=matches,
        created_by=getattr(user, "email", None) or "user",
    )


@router.get("/ranking")
async def get_ranking_endpoint(
    ranking_key: Optional[str] = Query(None),
    history: bool = Query(False),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Latest leaderboard for a ``ranking_key`` (``{subject}:{method}``); with
    ``history=true`` also returns the version history (newest first)."""
    from app.quality.ranking import get_ranking, list_rankings

    latest = await get_ranking(db, workspace_id=workspace.id, ranking_key=ranking_key)
    if not history:
        return latest
    versions = await list_rankings(db, workspace_id=workspace.id, ranking_key=ranking_key)
    return {"latest": latest, "history": versions}


@router.get("/ranking/badge")
async def ranking_badge(
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Compact badge: 'leaderboard of N players, top = X'."""
    from app.quality.ranking import get_ranking_badge

    return await get_ranking_badge(db, workspace_id=workspace.id)


# --------------------------------------------------------------------------- #
# Variance / Robustness Harness (E-11)
# --------------------------------------------------------------------------- #
class VarianceSpecBody(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: Optional[str] = None
    reference_answer: Optional[str] = None


class VarianceCreate(BaseModel):
    # Exactly one source: replay an existing task, or run a fresh spec.
    source_task_id: Optional[str] = None
    spec: Optional[VarianceSpecBody] = None
    n: int = Field(default=10, ge=2, le=50)
    parallel: bool = True
    cost_cap_usd: Optional[float] = Field(default=None, gt=0)
    template_id: Optional[str] = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "VarianceCreate":
        if (self.source_task_id is None) == (self.spec is None):
            raise ValueError("provide exactly one of source_task_id or spec")
        return self


def _parse_uuid(value: Optional[str], field: str) -> Optional[uuid.UUID]:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid {field}")


def _variance_run_out(run: VarianceRun, children: Optional[list[Task]] = None) -> dict:
    out = {
        "id": str(run.id),
        "workspace_id": str(run.workspace_id),
        "source_task_id": str(run.source_task_id) if run.source_task_id else None,
        "source_spec": run.source_spec,
        "template_id": str(run.template_id) if run.template_id else None,
        "n": run.n,
        "parallel": run.parallel,
        "cost_cap_usd": float(run.cost_cap_usd) if run.cost_cap_usd is not None else None,
        "status": run.status,
        "child_task_ids": run.child_task_ids,
        "accumulated_cost_usd": float(run.accumulated_cost_usd or 0),
        "aggregate": run.aggregate,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }
    if children is not None:
        out["children"] = [
            {
                "id": str(c.id),
                "status": c.status,
                "cost_usd": float(c.cost_usd or 0),
                "result_summary": (c.result_summary or "")[:200],
            }
            for c in children
        ]
    return out


@router.post("/variance")
async def create_variance_run(
    body: VarianceCreate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Start a variance run: replay a task (or a fresh spec) N times and measure
    the dispersion of outcome / trajectory / success / tool selection."""
    from app.quality.variance import run_variance

    try:
        run = await run_variance(
            db,
            workspace_id=workspace.id,
            source_task_id=_parse_uuid(body.source_task_id, "source_task_id"),
            source_spec=body.spec.model_dump() if body.spec else None,
            n=body.n,
            parallel=body.parallel,
            cost_cap_usd=Decimal(str(body.cost_cap_usd)) if body.cost_cap_usd is not None else None,
            template_id=_parse_uuid(body.template_id, "template_id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _variance_run_out(run)


@router.get("/variance")
async def list_variance_runs(
    source_task_id: Optional[str] = Query(None),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    q = select(VarianceRun).where(VarianceRun.workspace_id == workspace.id)
    sid = _parse_uuid(source_task_id, "source_task_id")
    if sid is not None:
        q = q.where(VarianceRun.source_task_id == sid)
    q = q.order_by(VarianceRun.created_at.desc())
    runs = (await db.execute(q)).scalars().all()
    return [_variance_run_out(r) for r in runs]


@router.get("/variance/{run_id}")
async def get_variance_run(
    run_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    rid = _parse_uuid(run_id, "run_id")
    run = await db.get(VarianceRun, rid)
    if run is None or run.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="variance run not found")
    children: list[Task] = []
    if run.child_task_ids:
        ids = [uuid.UUID(x) for x in run.child_task_ids]
        rows = (await db.execute(select(Task).where(Task.id.in_(ids)))).scalars().all()
        by_id = {str(t.id): t for t in rows}
        children = [by_id[i] for i in run.child_task_ids if i in by_id]
    return _variance_run_out(run, children=children)


# --------------------------------------------------------------------------- #
# Adversarial / Perturbation Judge (E-12)
# --------------------------------------------------------------------------- #
class PerturbationCreate(BaseModel):
    source_task_id: str
    transforms: Optional[list[str]] = None  # default: all four
    variants_per_transform: int = Field(default=1, ge=1, le=5)
    base_n: int = Field(default=2, ge=1, le=10)
    parallel: bool = True
    cost_cap_usd: Optional[float] = Field(default=None, gt=0)
    template_id: Optional[str] = None


def _perturbation_run_out(
    run: PerturbationRun, children_by_id: Optional[dict] = None
) -> dict:
    out = {
        "id": str(run.id),
        "workspace_id": str(run.workspace_id),
        "source_task_id": str(run.source_task_id) if run.source_task_id else None,
        "template_id": str(run.template_id) if run.template_id else None,
        "transforms": run.transforms,
        "variants_per_transform": run.variants_per_transform,
        "base_n": run.base_n,
        "parallel": run.parallel,
        "cost_cap_usd": float(run.cost_cap_usd) if run.cost_cap_usd is not None else None,
        "status": run.status,
        "base_task_ids": run.base_task_ids,
        "perturbed_task_ids": run.perturbed_task_ids,
        "accumulated_cost_usd": float(run.accumulated_cost_usd or 0),
        "aggregate": run.aggregate,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }
    if children_by_id is not None:
        from app.quality.perturbation import injection_followed

        def _child(cid: str, *, is_inject: bool) -> dict:
            c = children_by_id.get(cid)
            if c is None:
                return {"id": cid, "status": "missing"}
            entry = {
                "id": str(c.id),
                "status": c.status,
                "cost_usd": float(c.cost_usd or 0),
                "title": c.title,
                "result_summary": (c.result_summary or "")[:200],
            }
            if is_inject:
                entry["injection_followed"] = injection_followed(c, run.injection_canary)
            return entry

        out["base_children"] = [_child(i, is_inject=False) for i in (run.base_task_ids or [])]
        out["perturbed_children"] = {
            tk: [_child(i, is_inject=(tk == "inject")) for i in ids]
            for tk, ids in (run.perturbed_task_ids or {}).items()
        }
    return out


@router.post("/perturbation")
async def create_perturbation_run(
    body: PerturbationCreate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Start a perturbation run: replay a finished task under paraphrase / noise /
    reorder / injection transforms and compare against a clean baseline."""
    from app.quality.perturbation import run_perturbation

    try:
        run = await run_perturbation(
            db,
            workspace_id=workspace.id,
            source_task_id=uuid.UUID(body.source_task_id),
            transforms=body.transforms,
            variants_per_transform=body.variants_per_transform,
            base_n=body.base_n,
            parallel=body.parallel,
            cost_cap_usd=Decimal(str(body.cost_cap_usd)) if body.cost_cap_usd is not None else None,
            template_id=_parse_uuid(body.template_id, "template_id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _perturbation_run_out(run)


@router.get("/perturbation")
async def list_perturbation_runs(
    source_task_id: Optional[str] = Query(None),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    q = select(PerturbationRun).where(PerturbationRun.workspace_id == workspace.id)
    sid = _parse_uuid(source_task_id, "source_task_id")
    if sid is not None:
        q = q.where(PerturbationRun.source_task_id == sid)
    q = q.order_by(PerturbationRun.created_at.desc())
    runs = (await db.execute(q)).scalars().all()
    return [_perturbation_run_out(r) for r in runs]


@router.get("/perturbation/{run_id}")
async def get_perturbation_run(
    run_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    rid = _parse_uuid(run_id, "run_id")
    run = await db.get(PerturbationRun, rid)
    if run is None or run.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="perturbation run not found")
    from app.quality.perturbation import _all_child_ids

    children_by_id: dict = {}
    ids = _all_child_ids(run)
    if ids:
        uids = [uuid.UUID(x) for x in ids]
        rows = (await db.execute(select(Task).where(Task.id.in_(uids)))).scalars().all()
        children_by_id = {str(t.id): t for t in rows}
    return _perturbation_run_out(run, children_by_id=children_by_id)


# --------------------------------------------------------------------------- #
# Reproducibility Snapshot (E-20)
# --------------------------------------------------------------------------- #
async def _record_for_task(
    db: AsyncSession, task_id: str, workspace: Workspace
) -> QualityRecord:
    try:
        tid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid task id")
    rec = (
        await db.execute(
            select(QualityRecord).where(
                QualityRecord.task_id == tid,
                QualityRecord.workspace_id == workspace.id,
            )
        )
    ).scalar_one_or_none()
    if rec is None:
        raise HTTPException(status_code=404, detail="quality record not found")
    return rec


@router.get("/records/{task_id}/reproducibility")
async def get_reproducibility(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Read the experiment_snapshot (E-20) for a task, or null if not captured."""
    rec = await _record_for_task(db, task_id, workspace)
    return {"task_id": task_id, "reproducibility": rec.reproducibility}


@router.post("/records/{task_id}/capture-reproducibility")
async def capture_reproducibility_record(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """(Re)capture the experiment_snapshot for a task into its quality record (E-20).
    Skipped (snapshot null) when the run has no captured execution context."""
    from app.quality.reproducibility import capture_snapshot

    task = await _get_owned_task(db, task_id, workspace)
    snapshot = await capture_snapshot(db, task)
    if snapshot is None:
        return {
            "task_id": task_id,
            "reproducibility": None,
            "skipped": True,
            "detail": "no execution context to snapshot (no agent_spawned data)",
        }
    return {"task_id": task_id, "reproducibility": snapshot, "skipped": False}


@router.get("/reproducibility/diff")
async def diff_reproducibility(
    task_a: str = Query(..., description="first task id"),
    task_b: str = Query(..., description="second task id"),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Diff two tasks' experiment_snapshots (E-20): what changed between the runs."""
    from app.quality.reproducibility import diff_snapshots

    rec_a = await _record_for_task(db, task_a, workspace)
    rec_b = await _record_for_task(db, task_b, workspace)
    if not rec_a.reproducibility or not rec_b.reproducibility:
        raise HTTPException(
            status_code=404, detail="both tasks must have a reproducibility snapshot"
        )
    return diff_snapshots(rec_a.reproducibility, rec_b.reproducibility)


@router.post("/records/{task_id}/replay")
async def replay_reproducibility(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Replay a run from its snapshot (E-20): clone the task with a run_config
    derived from the captured state, linked via replay_of_task_id."""
    from app.quality.reproducibility import replay_from_snapshot

    task = await _get_owned_task(db, task_id, workspace)
    try:
        return await replay_from_snapshot(db, task.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
