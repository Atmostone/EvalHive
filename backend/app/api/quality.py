"""Quality Rubric Engine (E-02) API.

Workspace-scoped CRUD for rubrics, plus reading a task's quality profile and
triggering an on-demand evaluation. Auto-evaluation otherwise runs as the
`quality_judge_evaluate` scheduler job (gated by the `quality_eval_enabled`
setting).
"""

from __future__ import annotations

import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_workspace, require_role
from app.database import get_db
from app.models.quality_record import QualityRecord
from app.models.rubric import Rubric
from app.models.task import Task
from app.models.workspace import Workspace

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
