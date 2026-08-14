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
from app.models.experiment import Experiment, ExperimentRun
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
    # Who is rating (SPA-85). `human` is the caller themselves — what tools they
    # used while rating is deliberately not a field. The machine types are for
    # scripted annotators and carry a label instead of a user id.
    annotator_type: Literal["human", "llm_judge", "synthetic"] = "human"
    annotator_label: Optional[str] = Field(default=None, max_length=255)
    # The annotation session that served this annotator their bundle. It, not the
    # body, decides the protocol for a human rating.
    session_id: Optional[uuid.UUID] = None
    # Declarable ONLY by a scripted annotator, which owns its own protocol. For a
    # human rating these are ignored: an unverifiable self-report is worth nothing.
    blind_to_model: bool = False
    blind_to_judge: bool = False


class AnnotationSessionBody(BaseModel):
    """`blind` is the protocol declared up front — it decides what the session's
    bundle is allowed to contain, and is what gets recorded on the rating."""

    blind: bool = False


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


@router.get("/records/{task_id}/external-checker")
async def get_external_checker(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Executable-checker (E-23 / Toolathlon gold.external_eval) detail for a run:
    the pass/fail verdict plus the eval + preprocess container log tails, so a user
    can see WHY the checker failed — concrete evidence for the "checker is itself
    unreliable (~21%)" narrative. ``available=false`` for plain (non-checker) runs.
    Scoped to the workspace via the parent experiment. The exit code itself is not
    stored (verdict = exit==0); the log tail carries the actual error."""
    row = (
        await db.execute(
            select(ExperimentRun, Experiment)
            .join(Experiment, ExperimentRun.experiment_id == Experiment.id)
            .where(
                ExperimentRun.task_id == uuid.UUID(task_id),
                Experiment.workspace_id == workspace.id,
            )
        )
    ).first()
    if row is None or (
        row[0].external_verdict is None
        and not row[0].eval_log
        and not row[0].preprocess_log
    ):
        return {"task_id": task_id, "available": False}
    run, experiment = row
    # The human-readable config label (e.g. "M3") lives on the experiment's
    # configurations JSONB, keyed by config_key — surface it so the run breadcrumb
    # shows the label, not just the auto "cfg-NN" key.
    label = next(
        (
            c.get("label")
            for c in (experiment.configurations or [])
            if c.get("config_key") == run.config_key
        ),
        None,
    )
    return {
        "task_id": task_id,
        "available": True,
        "verdict": None
        if run.external_verdict is None
        else ("pass" if run.external_verdict else "fail"),
        "case_key": run.case_key,
        "config_key": run.config_key,
        "label": label,
        "launch_time": run.launch_time,
        "eval_log": run.eval_log,
        "preprocess_log": run.preprocess_log,
    }


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
    """The latest human rating (E-05) for a task, or null if none submitted yet.

    Readable by any workspace member — annotation stays visible to everyone who
    can see the task. Only the *write* is role-gated (SPA-85). This is the
    sighted view; a blind annotator reads the same material through the session
    bundle below."""
    from app.quality.feedback import get_human_feedback

    task = await _get_owned_task(db, task_id, workspace)
    return {"task_id": task_id, "human_feedback": await get_human_feedback(db, task)}


@router.get("/records/{task_id}/annotations")
async def get_annotations(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """The append-only annotation ledger for a task, oldest first (SPA-85).

    Every rating this run has ever carried, with its annotator, protocol, the
    judge observation frozen at that moment and the session it was produced
    through. `human_feedback` above is the materialised latest human row."""
    from app.quality.feedback import list_annotations

    task = await _get_owned_task(db, task_id, workspace)
    return {"task_id": task_id, "annotations": await list_annotations(db, task)}


@router.post("/records/{task_id}/annotation-session")
async def start_annotation_session(
    task_id: str,
    body: AnnotationSessionBody,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _role=Depends(require_role("owner", "admin")),
):
    """Open an annotation session and get everything needed to rate this run.

    The protocol is declared **before** anything is fetched and the bundle is
    built to match it in one place, so a blind session physically cannot receive
    a judge score. Submitting a rating with the returned `session_id` records the
    protocol from the session rather than from the request body, and consumes it.

    What the flag then means, precisely: «this rating was produced through a
    session that was served no judge scores». It is not a claim that the person
    never saw the judge — scores also reach a client from the evaluate endpoints,
    the experiment-results payload and the analytical surfaces, and a partial
    version of that claim would read as a guarantee while being false."""
    from app.quality.feedback import (
        annotation_bundle,
        feedback_from_annotation,
        list_annotations,
        open_annotation_session,
        own_annotation,
    )

    task = await _get_owned_task(db, task_id, workspace)
    rec = (
        await db.execute(
            select(QualityRecord).where(QualityRecord.task_id == task.id)
        )
    ).scalar_one_or_none()
    session = await open_annotation_session(
        db, task, user_id=user.id, blind=body.blind, commit=False
    )
    # This annotator's own rating — never the materialised `human_feedback`
    # slot, which holds whoever rated last. Serving the slot would hand a second
    # annotator the first one's scores to edit, and inter-annotator κ over that
    # measures agreement with a pre-filled form.
    mine = await own_annotation(db, task, user.id)
    bundle = annotation_bundle(
        review=await _review_context(db, task),
        quality_profile=rec.quality_profile if rec is not None else None,
        trajectory_profile=rec.trajectory_profile if rec is not None else None,
        own_feedback=feedback_from_annotation(mine) if mine is not None else None,
        annotations=await list_annotations(db, task),
        annotator_id=user.id,
        model_used=(rec.model_used if rec is not None else None) or task.model_used,
        blind=body.blind,
    )
    await db.commit()
    return {
        "session_id": str(session.id),
        "task_id": task_id,
        "protocol": {
            "protocol_version": session.protocol_version,
            "blind_to_judge": session.blind_to_judge,
            "blind_to_model": session.blind_to_model,
        },
        **bundle,
    }


@router.put("/records/{task_id}/feedback")
async def put_feedback(
    task_id: str,
    body: HumanFeedbackBody,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _role=Depends(require_role("owner", "admin")),
):
    """Append a rating of this task to the annotation ledger (E-05).

    A parallel signal that does not change the judge gate. Rating the same task
    again as the same annotator supersedes that annotator's previous row rather
    than the record's, so a second annotator's rating stays intact and the two
    can be compared. Role-gated: calibration data is what the reliability claims
    rest on, and any member could previously overwrite it."""
    from app.quality.feedback import (
        feedback_from_annotation,
        resolve_annotation_session,
        save_annotation,
    )

    task = await _get_owned_task(db, task_id, workspace)
    payload = body.model_dump()
    human = body.annotator_type == "human"
    # The protocol comes from the session that served this annotator their
    # bundle — never from the body, which cannot be verified. Omitting the
    # session means making no claim: the rating is recorded as sighted. A machine
    # annotator has no session and declares its own protocol.
    session = None
    if human and body.session_id:
        # Identity first, rating second. Resolving the rating by session id alone
        # would answer a replay of somebody else's id with their feedback — in
        # any workspace.
        session, existing = await resolve_annotation_session(
            db,
            body.session_id,
            task_id=task.id,
            workspace_id=workspace.id,
            user_id=user.id,
        )
        if session is None:
            # Unknown, or not this caller's / this run's / this workspace's.
            # Downgrading to sighted would record a protocol the caller did not
            # ask for, so this is a conflict, not a default.
            raise HTTPException(
                status_code=409,
                detail="annotation session is unknown or not yours",
            )
        if existing is not None:
            # A retry after a lost response finds its own rating rather than
            # writing a second one under a silently different protocol.
            return {
                "task_id": task_id,
                "human_feedback": feedback_from_annotation(existing),
            }
        if session.consumed_at is not None:
            raise HTTPException(
                status_code=409, detail="annotation session has already been used"
            )
    feedback = await save_annotation(
        db,
        task,
        payload,
        annotator_type=body.annotator_type,
        # A machine annotator has no user behind it, so it carries a label only —
        # counting it as a person is exactly the confusion this replaces.
        annotator_id=user.id if human else None,
        annotator_label=(user.email if human else body.annotator_label) or user.email,
        blind_to_model=False if human else body.blind_to_model,
        blind_to_judge=False if human else body.blind_to_judge,
        session=session,
    )
    return {"task_id": task_id, "human_feedback": feedback}


async def _review_context(db: AsyncSession, task) -> dict:
    """Everything a human annotator needs to judge a result: the task prompt
    (the question), the reference answer if any, the result summary, and text
    excerpts of the deliverable files — the same material the judge sees.

    Carries nothing judge-derived, so it is identical under either protocol and
    the session bundle reuses it verbatim."""
    import asyncio

    from app.storage.artifact_markdown import result_file_markdown
    from app.storage.minio_client import read_result_file_text

    async def _one(path: str) -> dict:
        """Text excerpt + converted Markdown for one deliverable. ``binary`` keeps
        its meaning (no text excerpt); ``markdown`` (SPA-71) is purely additive so
        the human sees converted docx/pdf/xlsx/etc. content, not just a note."""
        name = path.split("/", 2)[-1]  # strip the results/<task_id>/ prefix

        async def _text():
            try:
                return await asyncio.to_thread(read_result_file_text, path, 8000)
            except Exception:
                return None

        async def _md():
            try:
                return await asyncio.to_thread(result_file_markdown, path)
            except Exception:
                return None

        text, markdown = await asyncio.gather(_text(), _md())
        return {"name": name, "text": text, "binary": text is None, "markdown": markdown}

    paths = [str(f) for f in (task.result_files or [])][:6]
    files = list(await asyncio.gather(*[_one(p) for p in paths])) if paths else []
    return {
        "task_id": str(task.id),
        "title": task.title,
        "description": task.description,
        "reference_answer": task.reference_answer,
        "result_summary": task.result_summary,
        "files": files,
    }


@router.get("/records/{task_id}/review")
async def get_review_context(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Lets the calibration UI show what is being rated, not just the rubric
    axes. Shares its body with the session bundle."""
    task = await _get_owned_task(db, task_id, workspace)
    return await _review_context(db, task)


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


@router.get("/calibration/queue")
async def calibration_queue(
    status: Literal["pending", "done", "all"] = Query("pending"),
    limit: int = Query(300, ge=1, le=1000),
    blind: bool = Query(False),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Records eligible for human annotation (E-17 calibration): those carrying a
    judge profile. ``status=pending`` (default) returns only the ones still
    missing human feedback. Includes ``origin='experiment'`` tasks, which the
    task board hides — this is the only UI path to annotate them.

    ``blind=true`` withholds `model_used` and `weighted_score` (SPA-85): the queue
    is the first thing an annotator sees, so a blind campaign must not have the
    model and the judge's verdict sitting on the row. It is a convenience for the
    annotation surface — what a rating actually records comes from its session."""
    rows = (
        await db.execute(
            select(QualityRecord, Task.title, Task.origin)
            .join(Task, Task.id == QualityRecord.task_id)
            .where(
                QualityRecord.workspace_id == workspace.id,
                QualityRecord.quality_profile.isnot(None),
            )
            .order_by(QualityRecord.created_at.desc())
        )
    ).all()

    total = len(rows)
    done = sum(1 for rec, _t, _o in rows if rec.human_feedback is not None)
    items = []
    for rec, title, origin in rows:
        has_feedback = rec.human_feedback is not None
        if status == "pending" and has_feedback:
            continue
        if status == "done" and not has_feedback:
            continue
        profile = rec.quality_profile or {}
        items.append(
            {
                "task_id": str(rec.task_id),
                "title": title,
                "origin": origin,
                "template_name": rec.template_name,
                "model_used": None if blind else rec.model_used,
                "benchmark_suite": rec.benchmark_suite,
                "weighted_score": None if blind else profile.get("weighted_score"),
                "n_dimensions": sum(
                    1 for d in (profile.get("dimensions") or []) if d.get("status") == "scored"
                ),
                "has_feedback": has_feedback,
                "created_at": rec.created_at.isoformat() if rec.created_at else None,
            }
        )
        if len(items) >= limit:
            break
    return {"total": total, "done": done, "pending": total - done, "items": items}


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


# --------------------------------------------------------------------------- #
# Pairwise Comparison (E-21)
# --------------------------------------------------------------------------- #
class ComparisonCreate(BaseModel):
    subject: Literal["model", "template", "prompt"] = "model"
    task_a_id: str
    # Direct mode: an existing candidate B.
    task_b_id: Optional[str] = None
    # Generated mode: rerun source_task_id (defaults to task_a_id) with overrides.
    source_task_id: Optional[str] = None
    b_run_config: Optional[dict] = None  # {model_id?|template_id?|soul_md?|seed?|temperature?}
    judge_mode: Literal["llm", "human"] = "llm"

    @model_validator(mode="after")
    def _direct_or_generated(self) -> "ComparisonCreate":
        if self.task_b_id is None and not self.b_run_config:
            raise ValueError("provide task_b_id (direct) or b_run_config (generated)")
        return self


class HumanVerdictBody(BaseModel):
    verdict: Literal["a", "b", "tie"]
    reasoning: Optional[str] = None


class PairwiseLeaderboardBody(BaseModel):
    subject: Literal["model", "template"] = "model"
    method: Literal["bt", "elo"] = "elo"
    source: Literal["judge", "human"] = "judge"


@router.post("/comparison")
async def create_comparison_endpoint(
    body: ComparisonCreate,
    workspace: Workspace = Depends(get_current_workspace),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Create a pairwise comparison (E-21). Direct: two existing tasks (an ``llm``
    comparison is judged immediately). Generated: candidate B is produced by
    re-running ``source_task_id`` with ``b_run_config`` and judged on the tick."""
    from app.quality.comparison import _serialize, create_comparison

    try:
        comp = await create_comparison(
            db,
            workspace_id=workspace.id,
            subject=body.subject,
            task_a_id=_parse_uuid(body.task_a_id, "task_a_id"),
            task_b_id=_parse_uuid(body.task_b_id, "task_b_id"),
            source_task_id=_parse_uuid(body.source_task_id, "source_task_id"),
            b_run_config=body.b_run_config,
            judge_mode=body.judge_mode,
            created_by=getattr(user, "email", None) or "user",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize(comp)


@router.get("/comparison")
async def list_comparisons_endpoint(
    subject: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """List comparisons (newest first), optionally filtered by ``subject`` /
    ``status``, plus the judge↔human agreement over the returned set (E-17)."""
    from app.models.pairwise_comparison import PairwiseComparison
    from app.quality.comparison import _serialize, judge_agreement

    q = select(PairwiseComparison).where(PairwiseComparison.workspace_id == workspace.id)
    if subject:
        q = q.where(PairwiseComparison.subject == subject)
    if status:
        q = q.where(PairwiseComparison.status == status)
    q = q.order_by(PairwiseComparison.created_at.desc()).limit(100)
    rows = (await db.execute(q)).scalars().all()
    return {
        "comparisons": [_serialize(r) for r in rows],
        "agreement": judge_agreement(rows),
    }


@router.post("/comparison/leaderboard")
async def pairwise_leaderboard_endpoint(
    body: PairwiseLeaderboardBody | None = None,
    workspace: Workspace = Depends(get_current_workspace),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Turn judged comparisons into real matches and rank them via the E-19 engine
    → an ELO leaderboard (``ranking_report``, ``source="explicit"``) shown in the
    Leaderboard tab. No LLM call."""
    from app.quality.comparison import run_pairwise_leaderboard

    body = body or PairwiseLeaderboardBody()
    return await run_pairwise_leaderboard(
        db,
        workspace_id=workspace.id,
        subject=body.subject,
        method=body.method,
        source=body.source,
        created_by=getattr(user, "email", None) or "user",
    )


@router.get("/comparison/{comparison_id}")
async def get_comparison_endpoint(
    comparison_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """A comparison with its side-by-side payload (each side's title / model /
    answer / score) for the UI."""
    from app.quality.comparison import get_comparison

    cid = _parse_uuid(comparison_id, "comparison_id")
    out = await get_comparison(db, cid, workspace_id=workspace.id, with_sides=True)
    if out is None:
        raise HTTPException(status_code=404, detail="comparison not found")
    return out


@router.post("/comparison/{comparison_id}/judge")
async def judge_comparison_endpoint(
    comparison_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Force / redo the LLM judge (position-bias mitigated) for a ready comparison.
    Spends two LLM calls."""
    from app.quality.comparison import _serialize, judge_comparison_by_id

    cid = _parse_uuid(comparison_id, "comparison_id")
    try:
        comp = await judge_comparison_by_id(db, cid, workspace_id=workspace.id)
    except ValueError as e:
        msg = str(e)
        raise HTTPException(status_code=404 if "not found" in msg else 400, detail=msg)
    return _serialize(comp)


@router.put("/comparison/{comparison_id}/human-verdict")
async def human_verdict_endpoint(
    comparison_id: str,
    body: HumanVerdictBody,
    workspace: Workspace = Depends(get_current_workspace),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Record a human winner on the comparison (E-17 linkage). The judge verdict,
    if any, is preserved alongside for agreement tracking."""
    from app.quality.comparison import _serialize, record_human_verdict

    cid = _parse_uuid(comparison_id, "comparison_id")
    try:
        comp = await record_human_verdict(
            db,
            cid,
            verdict=body.verdict,
            reasoning=body.reasoning,
            submitted_by=getattr(user, "email", None) or "user",
            workspace_id=workspace.id,
        )
    except ValueError as e:
        msg = str(e)
        raise HTTPException(status_code=404 if "not found" in msg else 400, detail=msg)
    return _serialize(comp)
