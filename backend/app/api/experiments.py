"""Experiment Runner API (SPA-40).

Workspace-scoped CRUD + lifecycle for A/B matrix experiments: create (with
matrix expansion + dataset freezing), preview, run/pause/resume/cancel,
progress matrix, report (cached once terminal), per-cell results, clone and
CSV/JSON export. Writes are owner/admin-only.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, get_current_workspace, require_role
from app.database import get_db
from app.models.experiment import (
    Experiment,
    ExperimentRun,
)
from app.models.quality_record import QualityRecord
from app.models.task import Task
from app.models.user import User
from app.models.workspace import Workspace
from app.quality import experiments as service
from app.quality.experiment_report import compute_report

router = APIRouter(prefix="/api/experiments", tags=["experiments"])


class ExperimentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    dataset: dict
    configurations: list[dict] = Field(default_factory=list)
    axes: Optional[dict] = None
    n_runs_per_cell: int = Field(default=1, ge=1, le=service.MAX_N_RUNS)
    budget_limit_usd: Optional[float] = Field(default=None, gt=0)
    max_parallel: Optional[int] = Field(default=None, ge=1, le=10)
    eval_config: dict = Field(default_factory=dict)


class ExperimentClone(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    changes: Optional[dict] = None


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid experiment id")


async def _get_scoped(
    experiment_id: str, workspace: Workspace, db: AsyncSession
) -> Experiment:
    exp = await db.get(Experiment, _parse_uuid(experiment_id))
    if exp is None or exp.workspace_id != workspace.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "experiment not found")
    return exp


def serialize(exp: Experiment, *, include_details: bool = True) -> dict:
    out = {
        "id": str(exp.id),
        "name": exp.name,
        "description": exp.description,
        "status": exp.status,
        "dataset": exp.dataset,
        "n_cases": len(exp.dataset_cases or []),
        "n_configs": len(exp.configurations or []),
        "n_runs_per_cell": exp.n_runs_per_cell,
        "total_runs": len(exp.dataset_cases or [])
        * len(exp.configurations or [])
        * exp.n_runs_per_cell,
        "budget_limit_usd": float(exp.budget_limit_usd)
        if exp.budget_limit_usd is not None
        else None,
        "max_parallel": exp.max_parallel,
        "eval_config": exp.eval_config or {},
        "accumulated_cost_usd": float(exp.accumulated_cost_usd or 0),
        "has_report": exp.report is not None,
        "error": exp.error,
        "created_by": exp.created_by,
        "created_at": exp.created_at.isoformat() if exp.created_at else None,
        "started_at": exp.started_at.isoformat() if exp.started_at else None,
        "completed_at": exp.completed_at.isoformat() if exp.completed_at else None,
    }
    if include_details:
        out["configurations"] = exp.configurations
        out["dataset_cases"] = [
            {"case_key": c["case_key"], "title": c["title"]}
            for c in (exp.dataset_cases or [])
        ]
        out["matrix_spec"] = exp.matrix_spec
    return out


async def _load_runs(db: AsyncSession, exp: Experiment) -> list[ExperimentRun]:
    return (
        (
            await db.execute(
                select(ExperimentRun)
                .where(ExperimentRun.experiment_id == exp.id)
                .order_by(
                    ExperimentRun.config_key,
                    ExperimentRun.case_key,
                    ExperimentRun.run_index,
                )
            )
        )
        .scalars()
        .all()
    )


@router.get("")
async def list_experiments(
    status_filter: Optional[str] = Query(None, alias="status"),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    query = select(Experiment).where(Experiment.workspace_id == workspace.id)
    if status_filter:
        query = query.where(Experiment.status == status_filter)
    query = query.order_by(Experiment.created_at.desc())
    rows = (await db.execute(query)).scalars().all()
    return [serialize(e, include_details=False) for e in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_experiment(
    body: ExperimentCreate,
    workspace: Workspace = Depends(get_current_workspace),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    payload = body.model_dump()
    try:
        preview = await service.estimate_preview(
            db, workspace_id=workspace.id, payload=payload
        )
        exp = await service.create_experiment(
            db,
            workspace_id=workspace.id,
            payload=payload,
            created_by=getattr(user, "email", None) or "user",
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"experiment '{body.name}' already exists"
        )
    return {**serialize(exp), "preview": preview}


@router.post("/preview")
async def preview_experiment(
    body: ExperimentCreate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Stateless estimate: total runs, cost, duration, warnings."""
    try:
        return await service.estimate_preview(
            db, workspace_id=workspace.id, payload=body.model_dump()
        )
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.get("/{experiment_id}")
async def get_experiment(
    experiment_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Detail + the live progress matrix (per-cell status counts)."""
    exp = await _get_scoped(experiment_id, workspace, db)
    runs = await _load_runs(db, exp)
    cells: dict[tuple[str, str], dict] = {}
    totals: dict[str, int] = {}
    for r in runs:
        cell = cells.setdefault(
            (r.config_key, r.case_key),
            {"config_key": r.config_key, "case_key": r.case_key, "counts": {}},
        )
        cell["counts"][r.status] = cell["counts"].get(r.status, 0) + 1
        totals[r.status] = totals.get(r.status, 0) + 1
    return {
        **serialize(exp),
        "matrix": list(cells.values()),
        "run_totals": totals,
    }


@router.delete("/{experiment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_experiment(
    experiment_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    exp = await _get_scoped(experiment_id, workspace, db)
    if exp.status == "running":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "cancel the experiment before deleting it"
        )
    await db.delete(exp)
    await db.commit()


@router.post("/{experiment_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_experiment(
    experiment_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    exp = await _get_scoped(experiment_id, workspace, db)
    try:
        await service.start_experiment(db, exp)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    # Claim the first batch immediately; the scheduler tick takes over from here.
    await service.advance_experiment(db, exp)
    await db.refresh(exp)
    return serialize(exp)


@router.post("/{experiment_id}/pause", status_code=status.HTTP_202_ACCEPTED)
async def pause_experiment(
    experiment_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    exp = await _get_scoped(experiment_id, workspace, db)
    try:
        await service.pause_experiment(db, exp)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    return serialize(exp)


@router.post("/{experiment_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_experiment(
    experiment_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    exp = await _get_scoped(experiment_id, workspace, db)
    try:
        await service.resume_experiment(db, exp)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    return serialize(exp)


@router.post("/{experiment_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_experiment(
    experiment_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    exp = await _get_scoped(experiment_id, workspace, db)
    try:
        await service.cancel_experiment(db, exp)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    return serialize(exp)


@router.get("/{experiment_id}/report")
async def experiment_report(
    experiment_id: str,
    refresh: bool = False,
    method: str = Query("bt", pattern="^(bt|elo)$"),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """The assembled report. Cached on the experiment once it is terminal;
    a running experiment gets a fresh partial report on every call."""
    exp = await _get_scoped(experiment_id, workspace, db)
    terminal = exp.status in service.TERMINAL_EXPERIMENT
    cached = exp.report
    if (
        terminal
        and cached
        and not refresh
        and (cached.get("leaderboard") or {}).get("method") == method
    ):
        return cached
    report = await compute_report(db, exp, method=method, partial=not terminal)
    if terminal:
        exp.report = report
        await db.commit()
    return report


@router.get("/{experiment_id}/results")
async def experiment_results(
    experiment_id: str,
    config: Optional[str] = None,
    case: Optional[str] = None,
    run_index: Optional[int] = None,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Per-cell run rows with task state and eval profiles."""
    exp = await _get_scoped(experiment_id, workspace, db)
    runs = await _load_runs(db, exp)
    if config:
        runs = [r for r in runs if r.config_key == config]
    if case:
        runs = [r for r in runs if r.case_key == case]
    if run_index is not None:
        runs = [r for r in runs if r.run_index == run_index]

    task_ids = [r.task_id for r in runs if r.task_id]
    tasks: dict = {}
    records: dict = {}
    if task_ids:
        for t in (
            (await db.execute(select(Task).where(Task.id.in_(task_ids)))).scalars().all()
        ):
            tasks[t.id] = t
        for rec in (
            (
                await db.execute(
                    select(QualityRecord).where(QualityRecord.task_id.in_(task_ids))
                )
            )
            .scalars()
            .all()
        ):
            records[rec.task_id] = rec

    out = []
    for r in runs:
        task = tasks.get(r.task_id)
        rec = records.get(r.task_id)
        out.append(
            {
                "config_key": r.config_key,
                "case_key": r.case_key,
                "run_index": r.run_index,
                "status": r.status,
                "task_id": str(r.task_id) if r.task_id else None,
                "task_status": task.status if task else None,
                "result_summary": task.result_summary if task else None,
                "weighted_score": r.weighted_score,
                "trajectory_score": r.trajectory_score,
                "cost_usd": float(r.cost_usd or 0),
                "duration_seconds": r.duration_seconds,
                "quality_profile": rec.quality_profile if rec else None,
                "trajectory_profile": rec.trajectory_profile if rec else None,
                "repro_fingerprint": (rec.reproducibility or {}).get("fingerprint")
                if rec
                else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
        )
    return out


@router.post("/{experiment_id}/clone", status_code=status.HTTP_201_CREATED)
async def clone_experiment(
    experiment_id: str,
    body: ExperimentClone,
    workspace: Workspace = Depends(get_current_workspace),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """New draft from this experiment, optionally with changed parameters.
    Re-run = clone (no changes) + run."""
    exp = await _get_scoped(experiment_id, workspace, db)
    try:
        clone = await service.clone_experiment(
            db,
            exp,
            name=body.name,
            changes=body.changes,
            created_by=getattr(user, "email", None) or "user",
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "experiment name already exists")
    return serialize(clone)


@router.get("/{experiment_id}/export")
async def export_experiment(
    experiment_id: str,
    format: str = Query("json", pattern="^(json|csv)$"),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Flat per-run rows (pandas-friendly): config axes + scores + costs."""
    exp = await _get_scoped(experiment_id, workspace, db)
    runs = await _load_runs(db, exp)
    configs = {c["config_key"]: c for c in exp.configurations}

    task_ids = [r.task_id for r in runs if r.task_id]
    records: dict = {}
    if task_ids:
        for rec in (
            (
                await db.execute(
                    select(QualityRecord).where(QualityRecord.task_id.in_(task_ids))
                )
            )
            .scalars()
            .all()
        ):
            records[rec.task_id] = rec

    dim_keys: list[str] = []
    rows = []
    for r in runs:
        cfg = configs.get(r.config_key, {})
        rec = records.get(r.task_id)
        row = {
            "experiment_id": str(exp.id),
            "experiment_name": exp.name,
            "config_key": r.config_key,
            "config_label": cfg.get("label"),
            "orchestrator": bool(cfg.get("orchestrator")),
            "template_id": cfg.get("template_id"),
            "model_id": cfg.get("model_id"),
            "temperature": cfg.get("temperature"),
            "seed": cfg.get("seed"),
            "memory_mode": cfg.get("memory_mode"),
            "case_key": r.case_key,
            "run_index": r.run_index,
            "status": r.status,
            "weighted_score": r.weighted_score,
            "trajectory_score": r.trajectory_score,
            "cost_usd": float(r.cost_usd or 0),
            "duration_seconds": r.duration_seconds,
            "task_id": str(r.task_id) if r.task_id else None,
            "repro_fingerprint": (rec.reproducibility or {}).get("fingerprint")
            if rec
            else None,
        }
        profile = (rec.quality_profile or {}) if rec else {}
        for dim in profile.get("dimensions") or []:
            key, score = dim.get("key"), dim.get("score")
            if key is None:
                continue
            col = f"dim_{key}"
            if col not in dim_keys:
                dim_keys.append(col)
            row[col] = score
        rows.append(row)

    if format == "json":
        return rows

    base_cols = [
        "experiment_id", "experiment_name", "config_key", "config_label",
        "orchestrator", "template_id", "model_id", "temperature", "seed",
        "memory_mode", "case_key", "run_index", "status", "weighted_score",
        "trajectory_score", "cost_usd", "duration_seconds", "task_id",
        "repro_fingerprint",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=base_cols + dim_keys, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        for key, value in list(row.items()):
            if isinstance(value, (dict, list)):
                row[key] = json.dumps(value)
        writer.writerow(row)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="experiment-{exp.id}.csv"'
        },
    )
