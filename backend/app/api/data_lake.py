"""Quality Data Lake (E-01) query + export API.

Read-only access to the materialized `quality_records`: filterable listing, the
full per-task blob, group-by aggregation for typical analytical questions, and
admin-only bulk export (JSON / Parquet) of the flattened summary table.
"""

import io
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_workspace, require_role
from app.database import get_db
from app.models.quality_record import QualityRecord
from app.models.task import Task, TaskStatus
from app.models.workspace import Workspace
from app.storage.minio_client import read_quality_record

router = APIRouter(prefix="/api/data-lake", tags=["data-lake"])

_EXPORT_CAP = 100000
_GROUP_COLUMNS = {
    "template_name": QualityRecord.template_name,
    "model_used": QualityRecord.model_used,
    "final_status": QualityRecord.final_status,
}


def _record_summary(r: QualityRecord) -> dict:
    return {
        "task_id": str(r.task_id),
        "workspace_id": str(r.workspace_id),
        "schema_version": r.schema_version,
        "template_id": str(r.template_id) if r.template_id else None,
        "template_name": r.template_name,
        "model_used": r.model_used,
        "final_status": r.final_status,
        "is_decomposition_root": r.is_decomposition_root,
        "cost_usd": float(r.cost_usd) if r.cost_usd is not None else None,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "duration_seconds": r.duration_seconds,
        "tool_call_count": r.tool_call_count,
        "public_dataset_opt_in": r.public_dataset_opt_in,
        "record_s3_path": r.record_s3_path,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _apply_filters(stmt, *, model_used, final_status, template_id, from_dt, to_dt):
    if model_used:
        stmt = stmt.where(QualityRecord.model_used == model_used)
    if final_status:
        stmt = stmt.where(QualityRecord.final_status == final_status)
    if template_id:
        stmt = stmt.where(QualityRecord.template_id == uuid.UUID(template_id))
    if from_dt:
        stmt = stmt.where(QualityRecord.created_at >= datetime.fromisoformat(from_dt))
    if to_dt:
        stmt = stmt.where(QualityRecord.created_at <= datetime.fromisoformat(to_dt))
    return stmt


def _with_title(stmt, title_contains):
    """Join tasks to filter by title substring (the 'Coder: Python vs TS' lens)."""
    if title_contains:
        stmt = stmt.join(Task, Task.id == QualityRecord.task_id).where(
            Task.title.ilike(f"%{title_contains}%")
        )
    return stmt


@router.get("/records")
async def list_records(
    template_id: str | None = None,
    model_used: str | None = None,
    final_status: str | None = None,
    title_contains: str | None = None,
    from_dt: str | None = None,
    to_dt: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(QualityRecord).where(QualityRecord.workspace_id == workspace.id)
    stmt = _apply_filters(
        stmt, model_used=model_used, final_status=final_status,
        template_id=template_id, from_dt=from_dt, to_dt=to_dt,
    )
    stmt = _with_title(stmt, title_contains)
    stmt = stmt.order_by(QualityRecord.created_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return [_record_summary(r) for r in rows]


@router.get("/records/{task_id}")
async def get_record(
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

    summary = _record_summary(rec)
    blob = None
    if rec.record_s3_path:
        try:
            blob = json.loads(read_quality_record(rec.record_s3_path))
        except Exception:
            blob = None
    return {"summary": summary, "record": blob}


@router.get("/query")
async def query_records(
    group_by: str = Query(default="template_name"),
    model_used: str | None = None,
    final_status: str | None = None,
    template_id: str | None = None,
    title_contains: str | None = None,
    from_dt: str | None = None,
    to_dt: str | None = None,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    if group_by not in _GROUP_COLUMNS:
        raise HTTPException(
            status_code=400,
            detail=f"group_by must be one of {sorted(_GROUP_COLUMNS)}",
        )
    col = _GROUP_COLUMNS[group_by]
    approved = case((QualityRecord.final_status == TaskStatus.DONE.value, 1), else_=0)
    tokens = QualityRecord.input_tokens + QualityRecord.output_tokens

    stmt = select(
        col.label("group"),
        func.count(QualityRecord.id).label("count"),
        func.avg(QualityRecord.cost_usd).label("avg_cost_usd"),
        func.count(QualityRecord.cost_usd).label("cost_known_count"),
        func.avg(tokens).label("avg_tokens"),
        func.count(tokens).label("tokens_known_count"),
        func.avg(QualityRecord.duration_seconds).label("avg_duration_s"),
        func.avg(approved).label("approval_rate"),
    ).where(QualityRecord.workspace_id == workspace.id)
    stmt = _apply_filters(
        stmt, model_used=model_used, final_status=final_status,
        template_id=template_id, from_dt=from_dt, to_dt=to_dt,
    )
    stmt = _with_title(stmt, title_contains)
    stmt = stmt.group_by(col).order_by(func.count(QualityRecord.id).desc())

    rows = (await db.execute(stmt)).all()
    return [
        {
            "group": r.group,
            "count": int(r.count),
            "avg_cost_usd": float(r.avg_cost_usd) if r.avg_cost_usd is not None else None,
            "cost_known_count": int(r.cost_known_count),
            "avg_tokens": float(r.avg_tokens) if r.avg_tokens is not None else None,
            "tokens_known_count": int(r.tokens_known_count),
            "avg_duration_s": float(r.avg_duration_s) if r.avg_duration_s is not None else None,
            "approval_rate": float(r.approval_rate or 0),
        }
        for r in rows
    ]


@router.get("/export")
async def export_records(
    format: str = Query(default="json"),
    template_id: str | None = None,
    model_used: str | None = None,
    final_status: str | None = None,
    title_contains: str | None = None,
    from_dt: str | None = None,
    to_dt: str | None = None,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Bulk export of the flattened summary table (JSON or Parquet). Full
    per-task blobs are available individually via GET /records/{task_id}."""
    if format not in ("json", "parquet"):
        raise HTTPException(status_code=400, detail="format must be json or parquet")

    stmt = select(QualityRecord).where(QualityRecord.workspace_id == workspace.id)
    stmt = _apply_filters(
        stmt, model_used=model_used, final_status=final_status,
        template_id=template_id, from_dt=from_dt, to_dt=to_dt,
    )
    stmt = _with_title(stmt, title_contains)
    stmt = stmt.order_by(QualityRecord.created_at.desc()).limit(_EXPORT_CAP)
    rows = [_record_summary(r) for r in (await db.execute(stmt)).scalars().all()]

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    base = f"data_lake_{workspace.id}_{stamp}"

    if format == "json":
        body = json.dumps(rows, ensure_ascii=False).encode("utf-8")
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{base}.json"'},
        )

    # parquet
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.Table.from_pylist(rows)
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{base}.parquet"'},
    )
