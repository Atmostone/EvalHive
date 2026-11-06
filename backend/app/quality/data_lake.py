"""Quality Data Lake (E-01): assemble immutable, versioned execution records.

On terminal status a task's full execution is gathered from the durable sources
(`tasks`, `agent_events` incl. the `agent_spawned` snapshot, `agent_log_chunks`)
into a JSON blob stored in MinIO, with a queryable summary row in `quality_records`.

The record is the foundation for downstream eval features; its JSONB slots
(quality_profile/trajectory_profile/human_feedback/longitudinal/reproducibility)
are left NULL here and filled later (E-02/E-07/E-05/E-22/E-20).
"""

import asyncio
import json
import logging
import math
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_log import AgentLogChunk
from app.models.event import AgentEvent
from app.models.quality_record import QualityRecord
from app.models.task import Task

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ASSEMBLER_VERSION = "1.1"

# Cap the number of events embedded in the blob to keep it bounded; the full
# log already lives in the MinIO log archive referenced by the record.
_MAX_EVENTS = 2000


def _tokens(token_usage: dict) -> tuple[int | None, int | None, int | None]:
    """(input, output, reasoning). Reasoning is a SUBSET of output, and absent —
    not zero — for a model that does not reason or a provider that does not say."""
    if not token_usage:
        return None, None, None
    return (
        token_usage.get("input_tokens"),
        token_usage.get("output_tokens"),
        token_usage.get("reasoning_tokens"),
    )


def _duration_seconds(task: Task, attempt=None) -> int | None:
    if getattr(task, "current_attempt_id", None) is not None:
        value = ((attempt.result or {}).get("metadata", {}).get("runner_elapsed_seconds") if attempt else None)
        return int(round(value)) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    if task.started_at and task.completed_at:
        return int(round((task.completed_at - task.started_at).total_seconds()))
    return None


def _spawn_snapshot_from_events(events: list[AgentEvent]) -> dict:
    """Latest `agent_spawned` event payload — the captured state snapshot."""
    for ev in reversed(events):
        if ev.event_type == "agent_spawned" and ev.data:
            return dict(ev.data)
    return {}


def _safe_snapshot(task: Task, execution: dict) -> dict | None:
    """Best-effort reproducibility experiment_snapshot (E-20) for the record.

    Built from the already-assembled ``execution`` section (zero extra queries).
    Wrapped so a snapshot failure can never block the record build / eval."""
    try:
        from app.quality.reproducibility import assemble_snapshot

        return assemble_snapshot(task, execution)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"reproducibility snapshot failed for task {task.id}: {e}")
        return None


async def assemble_record(db: AsyncSession, task: Task) -> dict:
    """Gather the full execution blob for a task. Best-effort, never raises on
    missing optional data."""
    query = select(AgentEvent).where(AgentEvent.task_id == task.id)
    if task.current_attempt_id is not None:
        query = query.where(AgentEvent.data["attempt_id"].astext == str(task.current_attempt_id))
    events = (await db.execute(query.order_by(AgentEvent.created_at))).scalars().all()

    snapshot = _spawn_snapshot_from_events(events)

    # Tool-call sequence from log chunks (present only before compaction).
    #
    # One logical call, not one row. A row is a transport artefact: the agent
    # writes the call before running the tool and the result afterwards, and an
    # output over the transport cap is split further — so counting rows counted a
    # single call two or more times, and `tool_call_count` (which the report reads
    # as `steps_mean`) inflated accordingly. The grouping is the trace cleaner's,
    # imported rather than reimplemented, so the frozen count and the judge's step
    # list cannot drift apart. `content` is deliberately not selected — the blob
    # needs the call, not megabytes of output it already stores elsewhere.
    from app.quality.trace_cleaner import join_tool_call_parts

    attempt_view = None
    if task.current_attempt_id is not None:
        from app.harnesses.projection import load_attempt_view

        attempt_view = await load_attempt_view(db, task.current_attempt_id)
        from app.harnesses.provenance import execution_context

        snapshot = execution_context(attempt_view["attempt"])
        events = [event for event in events if (event.data or {}).get("attempt_id") == str(task.current_attempt_id)]
        tool_calls = [
            {key: call.get(key) for key in (
                "chunk_seq", "tool_name", "native_tool_name", "tool_aliases", "tool_call_id", "session_id",
                "parent_session_id", "parent_call_id", "arguments", "arguments_truncated",
                "result_missing", "start_missing", "tool_status",
            )}
            for call in attempt_view["chunks"] if call["tool_call_id"] is not None
        ]
        for call in tool_calls:
            call["seq"] = call.pop("chunk_seq")
    else:
        chunks = (
            await db.execute(
                select(
                    AgentLogChunk.chunk_seq,
                    AgentLogChunk.tool_name,
                    AgentLogChunk.tool_call_id,
                    AgentLogChunk.part_index,
                    AgentLogChunk.part_total,
                    AgentLogChunk.arguments,
                    AgentLogChunk.arguments_truncated,
                )
                .where(AgentLogChunk.task_id == task.id)
                .order_by(AgentLogChunk.chunk_seq)
            )
        ).all()
        rows = [
            {
                "chunk_seq": seq,
                "tool_name": tool_name,
                "tool_call_id": call_id,
                "part_index": part_index,
                "part_total": part_total,
                "arguments": arguments,
                "arguments_truncated": args_truncated,
            }
            for seq, tool_name, call_id, part_index, part_total, arguments, args_truncated in chunks
        ]
        seq_by_call = {r["tool_call_id"]: r["chunk_seq"] for r in reversed(rows)}
        tool_calls = [
            {
                "seq": seq_by_call.get(call["tool_call_id"], i),
                "tool_name": call["tool_name"],
                # The parameters, frozen with the call (SPA-86) — `parameter_quality`
                # cannot be re-examined from a blob that only knows the tool's name.
                "arguments": call["arguments"],
                "arguments_truncated": call["arguments_truncated"],
            }
            for i, call in enumerate(join_tool_call_parts(rows))
            if call["tool_name"]
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
                "cost_usd": float(c.cost_usd) if c.cost_usd is not None else None,
                "depends_on": [str(d) for d in (c.depends_on or [])],
            }
            for c in children
        ]
    if task.parent_id:
        decomposition["parent_id"] = str(task.parent_id)

    inp, out, reasoning = _tokens(task.token_usage or {})

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
            "cost_usd": float(task.cost_usd) if task.cost_usd is not None else None,
            "orchestrator_cost_usd": float(task.orchestrator_cost_usd or 0),
            "orchestrator_usage": task.orchestrator_usage or {},
            "input_tokens": inp,
            "output_tokens": out,
            "reasoning_tokens": reasoning,
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
            **({"harness": snapshot["harness"]} if "harness" in snapshot else {}),
            "tool_calls": tool_calls,
            "log_archive_s3_path": task.log_archive_s3_path,
            "attempt": {
                "id": str(attempt_view["attempt"].id),
                "number": attempt_view["attempt"].number,
                "connector_id": attempt_view["attempt"].connector_id,
                "connector_version": attempt_view["attempt"].connector_version,
                "protocol_version": attempt_view["attempt"].protocol_version,
                "completeness": attempt_view["completeness"],
                "usage": attempt_view["usage"],
            } if attempt_view else None,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "duration_seconds": _duration_seconds(task, attempt_view["attempt"] if attempt_view else None),
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

    # Serialize archiving against attempt creation, which resets task projections.
    task = await db.scalar(select(Task).where(Task.id == task.id).with_for_update().execution_options(populate_existing=True))

    existing = (
        await db.execute(
            select(QualityRecord).where(QualityRecord.task_id == task.id)
        )
    ).scalar_one_or_none()

    inp, out, reasoning = _tokens(task.token_usage or {})
    attempt = None
    if task.current_attempt_id is not None:
        from app.harnesses.attempts import TERMINAL
        from app.models.execution_attempt import ExecutionAttempt

        attempt = await db.get(ExecutionAttempt, task.current_attempt_id)
        if attempt is not None and attempt.status not in TERMINAL:
            return None
    duration = _duration_seconds(task, attempt)

    if existing is not None:
        if existing.execution_attempt_id != task.current_attempt_id:
            raise ValueError("Archived record and current attempt have different measurement sources")
        existing.final_status = task.status
        existing.cost_usd = task.cost_usd
        existing.orchestrator_cost_usd = task.orchestrator_cost_usd or 0
        existing.input_tokens = inp
        existing.output_tokens = out
        existing.reasoning_tokens = reasoning
        existing.duration_seconds = duration
        if commit:
            await db.commit()
        return existing

    blob = await assemble_record(db, task)
    content = json.dumps(blob, ensure_ascii=False, default=str).encode("utf-8")
    s3_path = await asyncio.to_thread(upload_quality_record, str(task.workspace_id), str(task.id), content)

    opt_in_default = bool(await get_setting(db, "data_lake_public_opt_in_default", False))

    record = QualityRecord(
        task_id=task.id,
        execution_attempt_id=task.current_attempt_id,
        workspace_id=task.workspace_id,
        schema_version=SCHEMA_VERSION,
        template_id=task.template_id,
        template_name=blob["execution"].get("template_name"),
        model_used=task.model_used,
        final_status=task.status,
        is_decomposition_root=blob["decomposition"].get("is_root", False),
        cost_usd=task.cost_usd,
        orchestrator_cost_usd=task.orchestrator_cost_usd or 0,
        input_tokens=inp,
        output_tokens=out,
        reasoning_tokens=reasoning,
        duration_seconds=duration,
        tool_call_count=len(blob["execution"].get("tool_calls", [])),
        benchmark_case_id=task.benchmark_case_id,
        benchmark_suite=task.benchmark_suite,
        record_s3_path=s3_path,
        public_dataset_opt_in=opt_in_default,
    )
    # E-20: capture the reproducibility snapshot automatically (best-effort).
    record.reproducibility = _safe_snapshot(task, blob["execution"])
    db.add(record)
    if commit:
        await db.commit()
        await db.refresh(record)
    logger.info(f"Built quality record for task {task.id} → {s3_path}")
    return record
