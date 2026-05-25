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
    rated dimension across all records that carry human feedback."""
    rows = (
        await db.execute(
            select(QualityRecord).where(
                QualityRecord.workspace_id == workspace.id,
                QualityRecord.human_feedback.isnot(None),
            )
        )
    ).scalars().all()

    out: list[dict] = []
    for r in rows:
        hf = r.human_feedback or {}
        judge = {d.get("key"): d for d in ((r.quality_profile or {}).get("dimensions") or [])}
        for d in hf.get("dimensions") or []:
            jd = judge.get(d.get("key")) or {}
            judge_score = d.get("judge_score")
            if judge_score is None:
                judge_score = jd.get("score")
            out.append(
                {
                    "task_id": str(r.task_id),
                    "dimension_key": d.get("key"),
                    "dimension_name": d.get("name"),
                    "judge_score": judge_score,
                    "human_score": d.get("score"),
                    "band": d.get("band"),
                    "judge_reasoning": jd.get("reasoning"),
                    "human_comment": d.get("comment"),
                    "verdict": hf.get("verdict"),
                    "submitted_at": hf.get("submitted_at"),
                }
            )
    return out


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
