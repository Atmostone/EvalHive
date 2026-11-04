"""CRUD for scheduled_jobs."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_workspace, require_role
from app.database import get_db
from app.models.scheduled_job import ScheduledJob
from app.models.workspace import Workspace
from app.scheduler import reload_jobs

router = APIRouter(prefix="/api/scheduled-jobs", tags=["scheduled-jobs"])


class ScheduledJobCreate(BaseModel):
    name: str
    kind: str  # cron | interval | once
    cron_expr: str | None = None
    interval_seconds: int | None = None
    fire_at: datetime | None = None
    payload: dict = {}
    enabled: bool = True


class ScheduledJobUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    cron_expr: str | None = None
    interval_seconds: int | None = None
    fire_at: datetime | None = None
    payload: dict | None = None
    enabled: bool | None = None


def _to_dict(j: ScheduledJob) -> dict:
    return {
        "id": str(j.id),
        "name": j.name,
        "kind": j.kind,
        "cron_expr": j.cron_expr,
        "interval_seconds": j.interval_seconds,
        "fire_at": j.fire_at.isoformat() if j.fire_at else None,
        "payload": j.payload or {},
        "enabled": j.enabled,
        "last_fired_at": j.last_fired_at.isoformat() if j.last_fired_at else None,
        "created_at": j.created_at.isoformat() if j.created_at else None,
    }


@router.get("")
async def list_jobs(
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(ScheduledJob)
            .where(ScheduledJob.workspace_id == workspace.id)
            .order_by(ScheduledJob.created_at.desc())
        )
    ).scalars().all()
    return [_to_dict(j) for j in rows]


@router.post(
    "",
    status_code=201,
    dependencies=[Depends(require_role("owner", "admin", "member"))],
)
async def create_job(
    body: ScheduledJobCreate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    if body.kind not in ("cron", "interval", "once"):
        raise HTTPException(status_code=400, detail="kind must be one of cron|interval|once")
    job = ScheduledJob(**body.model_dump(), workspace_id=workspace.id)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    await reload_jobs()
    return _to_dict(job)


async def _get_scoped_job(
    job_id: uuid.UUID, workspace: Workspace, db: AsyncSession
) -> ScheduledJob:
    job = await db.get(ScheduledJob, job_id)
    if not job or job.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.patch(
    "/{job_id}",
    dependencies=[Depends(require_role("owner", "admin", "member"))],
)
async def update_job(
    job_id: uuid.UUID,
    body: ScheduledJobUpdate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    job = await _get_scoped_job(job_id, workspace, db)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(job, k, v)
    await db.commit()
    await db.refresh(job)
    await reload_jobs()
    return _to_dict(job)


@router.delete(
    "/{job_id}",
    status_code=204,
    dependencies=[Depends(require_role("owner", "admin"))],
)
async def delete_job(
    job_id: uuid.UUID,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    job = await _get_scoped_job(job_id, workspace, db)
    await db.delete(job)
    await db.commit()
    await reload_jobs()
