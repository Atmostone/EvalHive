"""Quality Data Lake (E-01): assemble immutable, versioned execution records.

On terminal status a task's full execution is gathered from the durable sources
(`tasks`, `agent_events` incl. the `agent_spawned` snapshot, `agent_log_chunks`)
into a JSON blob stored in MinIO, with a queryable summary row in `quality_records`.

The record is the foundation for downstream eval features; its JSONB slots
(quality_profile/trajectory_profile/human_feedback/longitudinal/reproducibility)
are left NULL here and filled later (E-02/E-07/E-05/E-22/E-20).
"""

import json
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_log import AgentLogChunk
from app.models.event import AgentEvent
from app.models.quality_record import QualityRecord
from app.models.task import Task

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ASSEMBLER_VERSION = "1.0"

# Cap the number of events embedded in the blob to keep it bounded; the full
# log already lives in the MinIO log archive referenced by the record.
_MAX_EVENTS = 2000


def _tokens(token_usage: dict) -> tuple[int | None, int | None]:
    if not token_usage:
        return None, None
    return token_usage.get("input_tokens"), token_usage.get("output_tokens")


def _duration_seconds(task: Task) -> int | None:
    if task.started_at and task.completed_at:
        return int(round((task.completed_at - task.started_at).total_seconds()))
    return None


def _spawn_snapshot_from_events(events: list[AgentEvent]) -> dict:
    """Latest `agent_spawned` event payload — the captured state snapshot."""
    for ev in reversed(events):
        if ev.event_type == "agent_spawned" and ev.data:
            return dict(ev.data)
    return {}


async def assemble_record(db: AsyncSession, task: Task) -> dict:
    """Gather the full execution blob for a task. Best-effort, never raises on
    missing optional data."""
    events = (
        await db.execute(
            select(AgentEvent)
            .where(AgentEvent.task_id == task.id)
            .order_by(AgentEvent.created_at)
        )
    ).scalars().all()

    snapshot = _spawn_snapshot_from_events(events)

    # Tool-call sequence from log chunks (present only before compaction).
    chunks = (
        await db.execute(
            select(AgentLogChunk.chunk_seq, AgentLogChunk.tool_name)
            .where(AgentLogChunk.task_id == task.id)
            .order_by(AgentLogChunk.chunk_seq)
        )
    ).all()
    tool_calls = [
        {"seq": seq, "tool_name": tool_name}
        for seq, tool_name in chunks
        if tool_name
    ]

    # Decomposition tree.
    children = (
        await db.execute(select(Task).where(Task.parent_id == task.id))
    ).scalars().all()
    is_root = bool(children)
    decomposition: dict = {"is_root": is_root}
    if is_root:
        decomposition["subtasks"] = [
            {
                "id": str(c.id),
                "title": c.title,
                "status": c.status,
                "template_id": str(c.template_id) if c.template_id else None,
                "model_used": c.model_used,
                "cost_usd": float(c.cost_usd or 0),
                "depends_on": [str(d) for d in (c.depends_on or [])],
            }
            for c in children
        ]
    if task.parent_id:
        decomposition["parent_id"] = str(task.parent_id)

    inp, out = _tokens(task.token_usage or {})

    return {
        "schema_version": SCHEMA_VERSION,
        "task": {
            "id": str(task.id),
            "title": task.title,
            "description": task.description,
            "priority": task.priority,
            "status": task.status,
            "parent_id": str(task.parent_id) if task.parent_id else None,
            "depends_on": [str(d) for d in (task.depends_on or [])],
            "retry_count": task.retry_count,
            "user_feedback": task.user_feedback,
            "orchestrator_feedback": task.orchestrator_feedback,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "cost_usd": float(task.cost_usd or 0),
            "input_tokens": inp,
            "output_tokens": out,
        },
        "decomposition": decomposition,
        "execution": {
            "template_id": snapshot.get("template_id") or (str(task.template_id) if task.template_id else None),
            "template_name": snapshot.get("template_name"),
            "model_api_name": snapshot.get("model_api_name") or task.model_used,
            "input_price_per_1m_usd": snapshot.get("input_price_per_1m_usd"),
            "output_price_per_1m_usd": snapshot.get("output_price_per_1m_usd"),
            "soul_md": snapshot.get("soul_md", ""),
            "tools": snapshot.get("tools", []),
            "mcp_servers": snapshot.get("mcp_servers", []),
            "resource_limits": snapshot.get("resource_limits", {}),
            "memory_context": snapshot.get("memory_context", ""),
            "flat_memory": snapshot.get("flat_memory", {}),
            "tool_calls": tool_calls,
            "log_archive_s3_path": task.log_archive_s3_path,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "duration_seconds": _duration_seconds(task),
        },
        "artifacts": {
            "result_summary": task.result_summary,
            "result_files": list(task.result_files or []),
        },
        "events": [
            {
                "event_type": ev.event_type,
                "source": ev.source,
                "data": ev.data,
                "created_at": ev.created_at.isoformat() if ev.created_at else None,
            }
            for ev in events[:_MAX_EVENTS]
        ],
        "metadata": {
            "schema_version": SCHEMA_VERSION,
            "assembler_version": ASSEMBLER_VERSION,
            "assembled_at": datetime.utcnow().isoformat(),
            "events_truncated": len(events) > _MAX_EVENTS,
        },
        "slots": {
            "quality_profile": None,
            "trajectory_profile": None,
            "human_feedback": None,
            "longitudinal": None,
            "reproducibility": None,
        },
    }


async def build_quality_record(
    db: AsyncSession, task: Task, *, commit: bool = True
) -> QualityRecord | None:
    """Create the quality record for a task (idempotent).

    If a record already exists, only reconcile the mutable summary fields
    (final_status / cost / tokens / duration) — the blob stays immutable.
    Otherwise assemble the blob, upload it to MinIO and insert the row.
    """
    from app.api.settings import get_setting
    from app.storage.minio_client import upload_quality_record

    existing = (
        await db.execute(
            select(QualityRecord).where(QualityRecord.task_id == task.id)
        )
    ).scalar_one_or_none()

    inp, out = _tokens(task.token_usage or {})

    if existing is not None:
        existing.final_status = task.status
        existing.cost_usd = task.cost_usd or 0
        existing.input_tokens = inp
        existing.output_tokens = out
        existing.duration_seconds = _duration_seconds(task)
        if commit:
            await db.commit()
        return existing

    blob = await assemble_record(db, task)
    content = json.dumps(blob, ensure_ascii=False, default=str).encode("utf-8")
    s3_path = upload_quality_record(str(task.workspace_id), str(task.id), content)

    opt_in_default = bool(await get_setting(db, "data_lake_public_opt_in_default", False))

    record = QualityRecord(
        task_id=task.id,
        workspace_id=task.workspace_id,
        schema_version=SCHEMA_VERSION,
        template_id=task.template_id,
        template_name=blob["execution"].get("template_name"),
        model_used=task.model_used,
        final_status=task.status,
        is_decomposition_root=blob["decomposition"].get("is_root", False),
        cost_usd=task.cost_usd or 0,
        input_tokens=inp,
        output_tokens=out,
        duration_seconds=_duration_seconds(task),
        tool_call_count=len(blob["execution"].get("tool_calls", [])),
        benchmark_case_id=task.benchmark_case_id,
        benchmark_suite=task.benchmark_suite,
        record_s3_path=s3_path,
        public_dataset_opt_in=opt_in_default,
    )
    db.add(record)
    if commit:
        await db.commit()
        await db.refresh(record)
    logger.info(f"Built quality record for task {task.id} → {s3_path}")
    return record
