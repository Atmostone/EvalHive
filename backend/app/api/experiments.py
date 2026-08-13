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
import statistics
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
from app.models.event import AgentEvent
from app.models.quality_record import QualityRecord
from app.models.task import Task
from app.models.user import User
from app.models.workspace import Workspace
from app.quality import experiments as service
from app.quality.experiment_report import (
    SCHEMA_VERSION as REPORT_SCHEMA_VERSION,
    SELECTION_LATEST_VALID as REPORT_SELECTION_DEFAULT,
    compute_report,
)

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
    # SPA-69: isolated Toolathlon PG lanes for parallel runs (None → serial on the
    # shared toolathlon_pg). Capped at the number of provisioned lane containers.
    n_toolathlon_lanes: Optional[int] = Field(
        default=None, ge=1, le=service.MAX_TOOLATHLON_LANES
    )
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
    # Retired configurations keep their entry (and their lineage) but are no
    # longer part of the matrix, so they must not inflate the counts.
    live_configs = [c for c in (exp.configurations or []) if not c.get("retired_at")]
    out = {
        "id": str(exp.id),
        "name": exp.name,
        "description": exp.description,
        "status": exp.status,
        "dataset": exp.dataset,
        "n_cases": len(exp.dataset_cases or []),
        "n_configs": len(live_configs),
        "n_retired_configs": len(exp.configurations or []) - len(live_configs),
        "revision": exp.revision,
        "n_runs_per_cell": exp.n_runs_per_cell,
        "total_runs": len(exp.dataset_cases or [])
        * len(live_configs)
        * exp.n_runs_per_cell,
        "budget_limit_usd": float(exp.budget_limit_usd)
        if exp.budget_limit_usd is not None
        else None,
        "max_parallel": exp.max_parallel,
        "n_toolathlon_lanes": exp.n_toolathlon_lanes,
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


async def _load_runs(
    db: AsyncSession, exp: Experiment, *, include_retired: bool = False
) -> list[ExperimentRun]:
    """Cells of the experiment, retired ones excluded by default (SPA-84).

    The report counts only live cells, so every other read path has to agree —
    otherwise the progress matrix, the results table and the export each describe
    a different population than the report, which is the confusion this change
    exists to end.
    """
    stmt = select(ExperimentRun).where(ExperimentRun.experiment_id == exp.id)
    if not include_retired:
        stmt = stmt.where(ExperimentRun.retired_at.is_(None))
    return (
        (
            await db.execute(
                stmt.order_by(
                    ExperimentRun.config_key,
                    ExperimentRun.case_key,
                    ExperimentRun.run_index,
                )
            )
        )
        .scalars()
        .all()
    )


async def _human_scores_by_task(db: AsyncSession, task_ids: list) -> dict:
    """E-05 human annotations for the given task ids, keyed by task_id.

    Returns ``{task_id: {"overall": <mean dim score 0-10 or None>, "verdict":
    "approve"|"reject"|None}}`` for every run that carries human feedback — the
    third oracle (alongside the executable checker and the LLM judge) surfaced on
    the progress matrix and report charts."""
    if not task_ids:
        return {}
    rows = (
        await db.execute(
            select(QualityRecord.task_id, QualityRecord.human_feedback).where(
                QualityRecord.task_id.in_(task_ids),
                QualityRecord.human_feedback.isnot(None),
            )
        )
    ).all()
    out: dict = {}
    for task_id, hf in rows:
        dims = (hf or {}).get("dimensions") or []
        scores = [
            float(d["score"])
            for d in dims
            if isinstance(d.get("score"), (int, float))
        ]
        out[task_id] = {
            "overall": round(sum(scores) / len(scores), 1) if scores else None,
            "verdict": (hf or {}).get("verdict"),
        }
    return out


async def _profile_breakdown_by_task(db: AsyncSession, task_ids: list) -> dict:
    """Per-task rubric dimensions (E-02) + trajectory axes (E-07) as ``(name,
    score)`` lists, keyed by task_id — the raw material for the per-cell top/bottom
    breakdown on the progress matrix (which axis/dimension drags a cell down). Only
    scored tasks are queried (the caller passes task_ids that carry a score)."""
    if not task_ids:
        return {}
    rows = (
        await db.execute(
            select(
                QualityRecord.task_id,
                QualityRecord.quality_profile,
                QualityRecord.trajectory_profile,
            ).where(QualityRecord.task_id.in_(task_ids))
        )
    ).all()
    out: dict = {}
    for task_id, qp, tp in rows:
        # Skip entries with no usable label (both name and key missing/falsy) —
        # else a None name becomes a dict key and renders as "null" in the cell
        # tooltip (mirrors the guard in experiment_report.py).
        dims = [
            (name, float(d["score"]))
            for d in ((qp or {}).get("dimensions") or [])
            if isinstance(d.get("score"), (int, float))
            and (name := d.get("name") or d.get("key"))
        ]
        axes = [
            (name, float(a["score"]))
            for a in ((tp or {}).get("axes") or [])
            if isinstance(a.get("score"), (int, float))
            and (name := a.get("name") or a.get("key"))
        ]
        out[task_id] = {"dims": dims, "axes": axes}
    return out


def _top_bottom(samples: dict[str, list[float]]) -> list[dict]:
    """``{name: [scores]}`` → ``[{name, mean}]`` sorted worst-first, so the UI can
    read off the dragging axis/dimension (head) and the strongest (tail)."""
    rows = [
        {"name": name, "mean": round(sum(vals) / len(vals), 1)}
        for name, vals in samples.items()
        if vals
    ]
    rows.sort(key=lambda r: r["mean"])
    return rows


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
    human_by_task = await _human_scores_by_task(db, [r.task_id for r in runs if r.task_id])
    # Per-cell rubric/axis breakdown is built only from scored runs (those carrying
    # a weighted/trajectory score), keeping this hot, polled endpoint's extra query
    # bounded to records that actually contribute.
    scored_task_ids = [
        r.task_id
        for r in runs
        if r.task_id
        and (r.weighted_score is not None or r.trajectory_score is not None)
    ]
    breakdown_by_task = await _profile_breakdown_by_task(db, scored_task_ids)
    cells: dict[tuple[str, str], dict] = {}
    totals: dict[str, int] = {}
    for r in runs:
        cell = cells.setdefault(
            (r.config_key, r.case_key),
            {
                "config_key": r.config_key,
                "case_key": r.case_key,
                "counts": {},
                "_q": [],
                "_t": [],
                "_h": [],
                "_dims": {},
                "_axes": {},
                "external_pass": 0,
                "external_total": 0,
                "human_rated": 0,
                "human_approve": 0,
            },
        )
        cell["counts"][r.status] = cell["counts"].get(r.status, 0) + 1
        totals[r.status] = totals.get(r.status, 0) + 1
        bd = breakdown_by_task.get(r.task_id)
        if r.weighted_score is not None:
            cell["_q"].append(float(r.weighted_score))
            # Per-dimension breakdown drawn from the SAME scored runs as the cell
            # quality_mean, so the cell tooltip is consistent with the q value.
            if bd is not None:
                for name, score in bd["dims"]:
                    cell["_dims"].setdefault(name, []).append(score)
        if r.trajectory_score is not None:
            cell["_t"].append(float(r.trajectory_score))
            if bd is not None:
                for name, score in bd["axes"]:
                    cell["_axes"].setdefault(name, []).append(score)
        if r.external_verdict is not None:  # Toolathlon executable verdict
            cell["external_total"] += 1
            if r.external_verdict:
                cell["external_pass"] += 1
        human = human_by_task.get(r.task_id)  # E-05 human annotation, if any
        if human is not None:
            cell["human_rated"] += 1
            if human["overall"] is not None:
                cell["_h"].append(human["overall"])
            if human["verdict"] == "approve":
                cell["human_approve"] += 1
    matrix = []
    for cell in cells.values():
        q = cell.pop("_q")
        t = cell.pop("_t")
        h = cell.pop("_h")
        dims = cell.pop("_dims")
        axes = cell.pop("_axes")
        cell["quality_mean"] = round(sum(q) / len(q), 2) if q else None
        cell["trajectory_mean"] = round(sum(t) / len(t), 2) if t else None
        cell["human_mean"] = round(sum(h) / len(h), 1) if h else None
        # Spread across runs in the cell (population std, like the report heatmap):
        # a stable score (σ≈0) vs a noisy one (σ large). None until ≥2 samples.
        cell["quality_std"] = round(statistics.pstdev(q), 2) if len(q) >= 2 else None
        cell["trajectory_std"] = round(statistics.pstdev(t), 2) if len(t) >= 2 else None
        cell["human_std"] = round(statistics.pstdev(h), 1) if len(h) >= 2 else None
        cell["dim_means"] = _top_bottom(dims)
        cell["axis_means"] = _top_bottom(axes)
        matrix.append(cell)
    return {
        **serialize(exp),
        "matrix": matrix,
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


@router.post("/{experiment_id}/retry-failed", status_code=status.HTTP_202_ACCEPTED)
async def retry_failed_experiment(
    experiment_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Reset cells that errored out (rate-limit / transient API / infra) back to
    pending and re-run them — no clone, valid cells untouched. The superseded
    state of each retried cell is kept in the attempt ledger. Repeatable across
    provider quota windows. 409 unless the experiment is settled or paused."""
    exp = await _get_scoped(experiment_id, workspace, db)
    try:
        retried = await service.retry_failed_experiment(db, exp)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    return {**serialize(exp), "retried": retried}


@router.post("/{experiment_id}/add-config", status_code=status.HTTP_202_ACCEPTED)
async def add_config(
    experiment_id: str,
    body: dict,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Add a configuration (e.g. another model) to an existing experiment and
    materialize+run its cells in place over the same frozen dataset — no clone."""
    exp = await _get_scoped(experiment_id, workspace, db)
    try:
        result = await service.add_config_to_experiment(db, exp, body)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    return {**serialize(exp), **result}


@router.delete("/{experiment_id}/configs/{config_key}", status_code=status.HTTP_200_OK)
async def remove_config(
    experiment_id: str,
    config_key: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Retire a configuration (e.g. a model that exhausted its quota) — inverse of
    add-config. Its cells keep their lineage but leave the matrix and the report.
    409 while running, on the last live configuration, or if already retired."""
    exp = await _get_scoped(experiment_id, workspace, db)
    try:
        result = await service.remove_config_from_experiment(db, exp, config_key)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    return {**serialize(exp), **result}


@router.get("/{experiment_id}/report")
async def experiment_report(
    experiment_id: str,
    refresh: bool = False,
    method: str = Query("bt", pattern="^(bt|elo)$"),
    selection: str = Query(
        REPORT_SELECTION_DEFAULT, pattern="^(latest_valid|all_attempts|first_attempt)$"
    ),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """The assembled report. Cached on the experiment once it is terminal;
    a running experiment gets a fresh partial report on every call.

    ``selection`` chooses which executions of each cell are counted:
    ``latest_valid`` (default) the current state of every live cell,
    ``all_attempts`` every execution including retired cells, ``first_attempt``
    each cell as it first came out. Only the default is ever cached — the others
    are analysis views, not the experiment's headline numbers.
    """
    exp = await _get_scoped(experiment_id, workspace, db)
    terminal = exp.status in service.TERMINAL_EXPERIMENT
    cached = exp.report
    cacheable = selection == REPORT_SELECTION_DEFAULT
    if (
        terminal
        and cached
        and not refresh
        and cacheable
        and cached.get("schema_version") == REPORT_SCHEMA_VERSION
        # The report must have been built from the experiment as it stands now.
        # Without this a mutated experiment kept serving its pre-mutation numbers
        # once it settled again, and the cache was formally valid (SPA-84).
        and cached.get("input_revision") == exp.revision
        # Recomputed, not read from the stored column: the column moves only
        # when a mutation moves it, so comparing it to itself would just repeat
        # the revision check and never catch input changed by any other route.
        and cached.get("input_fingerprint") == service.experiment_input_fingerprint(exp)
        and (cached.get("leaderboard") or {}).get("method") == method
    ):
        return cached
    report = await compute_report(
        db, exp, method=method, partial=not terminal, selection=selection
    )
    if terminal and cacheable:
        exp.report = report
        await db.commit()
    return report


@router.get("/{experiment_id}/results")
async def experiment_results(
    experiment_id: str,
    config: Optional[str] = None,
    case: Optional[str] = None,
    run_index: Optional[int] = None,
    include_retired: bool = False,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Per-cell run rows with task state and eval profiles.

    Cells of a retired configuration are excluded unless ``include_retired`` —
    their lineage is kept, so this is the way back to it."""
    exp = await _get_scoped(experiment_id, workspace, db)
    runs = await _load_runs(db, exp, include_retired=include_retired)
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

    # Executable verdicts (Toolathlon external_eval_verdict events): latest per task.
    verdicts: dict = {}
    if task_ids:
        for tid, data in (
            await db.execute(
                select(AgentEvent.task_id, AgentEvent.data)
                .where(
                    AgentEvent.task_id.in_(task_ids),
                    AgentEvent.event_type == "external_eval_verdict",
                )
                .order_by(AgentEvent.created_at)
            )
        ).all():
            verdicts[tid] = bool((data or {}).get("passed"))

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
                "external_verdict": (
                    ("pass" if verdicts[r.task_id] else "fail")
                    if r.task_id in verdicts
                    else None
                ),
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
    include_retired: bool = False,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Flat per-run rows (pandas-friendly): config axes + scores + costs.

    Matches the report's population by default; ``include_retired`` widens it to
    the cells of retired configurations."""
    exp = await _get_scoped(experiment_id, workspace, db)
    runs = await _load_runs(db, exp, include_retired=include_retired)
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
