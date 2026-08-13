"""Aggregated analytics over tasks (per-template, per-model, timeline)."""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_workspace
from app.database import get_db
from app.models.experiment import Experiment, ExperimentRun, ExperimentRunStatus
from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.models.workspace import Workspace
from app.quality.experiments import LIVE_CELL

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _resolve_period(period: str) -> Optional[datetime]:
    if period == "all":
        return None
    now = datetime.utcnow()
    return {
        "day": now - timedelta(days=1),
        "week": now - timedelta(days=7),
        "month": now - timedelta(days=30),
    }.get(period, now - timedelta(days=7))


def _duration_seconds_expr():
    return func.coalesce(
        func.extract("epoch", Task.completed_at - Task.started_at),
        0,
    )


@router.get("/templates")
async def template_analytics(
    period: str = Query(default="week"),
    from_dt: Optional[str] = None,
    to_dt: Optional[str] = None,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    cutoff = _resolve_period(period)

    where_clauses = [Template.workspace_id == workspace.id]
    if from_dt:
        where_clauses.append(Task.created_at >= datetime.fromisoformat(from_dt))
    elif cutoff is not None:
        where_clauses.append(Task.created_at >= cutoff)
    if to_dt:
        where_clauses.append(Task.created_at <= datetime.fromisoformat(to_dt))

    approved = case((Task.status == TaskStatus.DONE.value, 1), else_=0)
    failed = case((Task.status == TaskStatus.FAILED.value, 1), else_=0)
    inp = func.coalesce(Task.token_usage["input_tokens"].as_integer(), 0)
    out = func.coalesce(Task.token_usage["output_tokens"].as_integer(), 0)

    stmt = (
        select(
            Template.id,
            Template.name,
            func.count(Task.id).label("task_count"),
            func.avg(approved).label("approval_rate"),
            func.avg(case((Task.retry_count > 0, 1), else_=0)).label("retry_rate"),
            func.avg(failed).label("failure_rate"),
            func.avg(_duration_seconds_expr()).label("avg_time_seconds"),
            func.avg(inp).label("avg_input_tokens"),
            func.avg(out).label("avg_output_tokens"),
            func.coalesce(func.sum(Task.cost_usd), 0).label("total_cost_usd"),
        )
        .join(Task, Task.template_id == Template.id, isouter=True)
        .group_by(Template.id, Template.name)
    )
    if where_clauses:
        stmt = stmt.where(*where_clauses)

    rows = (await db.execute(stmt)).all()
    out_list = []
    for r in rows:
        count = int(r.task_count or 0)
        out_list.append({
            "template_id": str(r.id),
            "template_name": r.name,
            "task_count": count,
            "approval_rate": float(r.approval_rate or 0),
            "retry_rate": float(r.retry_rate or 0),
            "failure_rate": float(r.failure_rate or 0),
            "avg_time_seconds": float(r.avg_time_seconds or 0),
            "avg_input_tokens": float(r.avg_input_tokens or 0),
            "avg_output_tokens": float(r.avg_output_tokens or 0),
            "total_cost_usd": float(r.total_cost_usd or 0),
            "cost_per_task_usd": (float(r.total_cost_usd or 0) / count) if count else 0,
        })
    return out_list


@router.get("/configs")
async def config_analytics(
    period: str = Query(default="week"),
    from_dt: Optional[str] = None,
    to_dt: Optional[str] = None,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Per-config aggregates across the workspace's experiments (experiment ×
    config_key) — configs are the A/B unit people actually run, vs the legacy
    per-template view. One row per (experiment, config_key)."""
    cutoff = _resolve_period(period)
    where = [Experiment.workspace_id == workspace.id]
    if from_dt:
        where.append(ExperimentRun.created_at >= datetime.fromisoformat(from_dt))
    elif cutoff is not None:
        where.append(ExperimentRun.created_at >= cutoff)
    if to_dt:
        where.append(ExperimentRun.created_at <= datetime.fromisoformat(to_dt))

    # Cells of a retired configuration are out of the matrix everywhere else —
    # the report, /results, /export and the CLI all skip them — so aggregating
    # them here would make this the one place describing a wider population.
    where.append(LIVE_CELL)

    s_success = ExperimentRunStatus.SUCCESS.value
    s_failed = ExperimentRunStatus.FAILED.value
    settled = case((ExperimentRun.status.in_([s_success, s_failed]), 1), else_=0)
    success = case((ExperimentRun.status == s_success, 1), else_=0)
    failed = case((ExperimentRun.status == s_failed, 1), else_=0)
    ev_pass = case((ExperimentRun.external_verdict.is_(True), 1), else_=0)
    ev_eval = case((ExperimentRun.external_verdict.isnot(None), 1), else_=0)

    stmt = (
        select(
            ExperimentRun.experiment_id,
            ExperimentRun.config_key,
            Experiment.name.label("experiment_name"),
            func.count(ExperimentRun.id).label("run_count"),
            func.sum(settled).label("settled"),
            func.sum(success).label("success"),
            func.sum(failed).label("failed"),
            func.avg(ExperimentRun.weighted_score).label("quality_mean"),
            func.avg(ExperimentRun.trajectory_score).label("trajectory_mean"),
            func.sum(ev_pass).label("ev_pass"),
            func.sum(ev_eval).label("ev_eval"),
            func.avg(ExperimentRun.duration_seconds).label("avg_time_seconds"),
            func.avg(ExperimentRun.cost_usd).label("avg_cost_usd"),
        )
        .join(Experiment, Experiment.id == ExperimentRun.experiment_id)
        .where(*where)
        .group_by(ExperimentRun.experiment_id, ExperimentRun.config_key, Experiment.name)
    )
    rows = (await db.execute(stmt)).all()

    # Resolve per-config display labels from each experiment's configurations JSON.
    exp_ids = {r.experiment_id for r in rows}
    labels: dict = {}
    if exp_ids:
        cfg_rows = (
            await db.execute(
                select(Experiment.id, Experiment.configurations).where(Experiment.id.in_(exp_ids))
            )
        ).all()
        for eid, configs in cfg_rows:
            for c in (configs or []):
                if isinstance(c, dict) and c.get("config_key"):
                    labels[(eid, c["config_key"])] = c.get("label")

    out_list = []
    for r in rows:
        settled_n = int(r.settled or 0)
        eval_n = int(r.ev_eval or 0)
        label = labels.get((r.experiment_id, r.config_key))
        name = f"{r.experiment_name} · {r.config_key}"
        if label:
            name += f" ({label})"
        out_list.append({
            "config_id": f"{r.experiment_id}:{r.config_key}",
            "config_name": name,
            "run_count": int(r.run_count or 0),
            "success_rate": (int(r.success or 0) / settled_n) if settled_n else 0.0,
            "failure_rate": (int(r.failed or 0) / settled_n) if settled_n else 0.0,
            "quality_mean": float(r.quality_mean) if r.quality_mean is not None else 0.0,
            "trajectory_mean": float(r.trajectory_mean) if r.trajectory_mean is not None else 0.0,
            "pass_rate": (int(r.ev_pass or 0) / eval_n) if eval_n else 0.0,
            "avg_time_seconds": float(r.avg_time_seconds or 0),
            "avg_cost_usd": float(r.avg_cost_usd or 0),
        })
    out_list.sort(key=lambda x: -x["run_count"])
    return out_list


@router.get("/timeline")
async def timeline(
    days: int = Query(default=30, ge=1, le=365),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.utcnow() - timedelta(days=days)
    inp = func.coalesce(Task.token_usage["input_tokens"].as_integer(), 0)
    out = func.coalesce(Task.token_usage["output_tokens"].as_integer(), 0)
    bucket = func.date_trunc("day", Task.created_at).label("day")
    stmt = (
        select(
            bucket,
            func.count(Task.id).label("task_count"),
            func.coalesce(func.sum(Task.cost_usd), 0).label("total_cost_usd"),
            func.coalesce(func.sum(inp + out), 0).label("total_tokens"),
        )
        .where(
            Task.created_at >= since,
            Task.workspace_id == workspace.id,
        )
        .group_by(bucket)
        .order_by(bucket)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "date": r.day.date().isoformat() if r.day else None,
            "task_count": int(r.task_count),
            "total_cost_usd": float(r.total_cost_usd),
            "total_tokens": int(r.total_tokens or 0),
        }
        for r in rows
    ]


@router.get("/models")
async def model_analytics(
    period: str = Query(default="week"),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    cutoff = _resolve_period(period)
    inp = func.coalesce(Task.token_usage["input_tokens"].as_integer(), 0)
    out = func.coalesce(Task.token_usage["output_tokens"].as_integer(), 0)
    stmt = (
        select(
            Task.model_used,
            func.count(Task.id).label("task_count"),
            func.coalesce(func.sum(Task.cost_usd), 0).label("total_cost_usd"),
            func.avg(inp).label("avg_input_tokens"),
            func.avg(out).label("avg_output_tokens"),
        )
        .where(Task.workspace_id == workspace.id)
        .group_by(Task.model_used)
    )
    if cutoff is not None:
        stmt = stmt.where(Task.created_at >= cutoff)
    rows = (await db.execute(stmt)).all()
    return [
        {
            "model": r.model_used or "unknown",
            "task_count": int(r.task_count),
            "total_cost_usd": float(r.total_cost_usd),
            "avg_input_tokens": float(r.avg_input_tokens or 0),
            "avg_output_tokens": float(r.avg_output_tokens or 0),
        }
        for r in rows
    ]
