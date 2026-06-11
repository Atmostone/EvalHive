import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_workspace
from app.database import get_db
from app.models.task import Task, TaskPriority, TaskStatus
from app.models.workspace import Workspace
from app.schemas.decomposition import DecompositionResponse
from app.utils.events import log_event

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    priority: str = TaskPriority.MEDIUM.value
    parent_id: Optional[str] = None
    reference_answer: Optional[str] = None
    canonical_trajectory: Optional[Any] = None
    capability_spec: Optional[Any] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    reference_answer: Optional[str] = None
    canonical_trajectory: Optional[Any] = None
    capability_spec: Optional[Any] = None


class TaskOut(BaseModel):
    id: str
    parent_id: Optional[str]
    title: str
    description: Optional[str]
    status: str
    priority: str
    template_id: Optional[str]
    agent_container_id: Optional[str]
    result_summary: Optional[str]
    result_files: list
    token_usage: dict
    retry_count: int
    max_retries: int
    user_feedback: Optional[str]
    orchestrator_feedback: Optional[str]
    created_at: str
    updated_at: str
    started_at: Optional[str]
    completed_at: Optional[str]

    class Config:
        from_attributes = True


def task_to_dict(task: Task) -> dict:
    return {
        "id": str(task.id),
        "parent_id": str(task.parent_id) if task.parent_id else None,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "priority": task.priority,
        "origin": task.origin,
        "template_id": str(task.template_id) if task.template_id else None,
        "agent_container_id": task.agent_container_id,
        "result_summary": task.result_summary,
        "reference_answer": task.reference_answer,
        "canonical_trajectory": task.canonical_trajectory,
        "capability_spec": task.capability_spec,
        "benchmark_case_id": task.benchmark_case_id,
        "benchmark_suite": task.benchmark_suite,
        "result_files": task.result_files,
        "token_usage": task.token_usage,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
        "user_feedback": task.user_feedback,
        "orchestrator_feedback": task.orchestrator_feedback,
        "model_used": task.model_used,
        "cost_usd": float(task.cost_usd) if task.cost_usd is not None else 0.0,
        "depends_on": [str(d) for d in (task.depends_on or [])],
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


async def _get_scoped_task(task_id: str, workspace: Workspace, db: AsyncSession) -> Task:
    task = await db.get(Task, uuid.UUID(task_id))
    if not task or task.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("")
async def list_tasks(
    status: Optional[str] = None,
    parent_id: Optional[str] = None,
    include_experiments: bool = False,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    query = select(Task).where(Task.workspace_id == workspace.id)
    # Benchmark children (SPA-40) are programmatic runs, not board work —
    # hidden unless explicitly requested.
    if not include_experiments:
        query = query.where(Task.origin != "experiment")
    if status:
        query = query.where(Task.status == status)
    if parent_id:
        query = query.where(Task.parent_id == uuid.UUID(parent_id))
    query = query.order_by(Task.created_at.desc())
    result = await db.execute(query)
    tasks = result.scalars().all()
    return [task_to_dict(t) for t in tasks]


@router.post("", status_code=201)
async def create_task(
    body: TaskCreate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    task = Task(
        title=body.title,
        description=body.description,
        priority=body.priority,
        parent_id=uuid.UUID(body.parent_id) if body.parent_id else None,
        reference_answer=body.reference_answer,
        canonical_trajectory=body.canonical_trajectory,
        capability_spec=body.capability_spec,
        workspace_id=workspace.id,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    await log_event(
        db, "task_created", "user", {"title": task.title},
        task_id=task.id, workspace_id=workspace.id,
    )
    return task_to_dict(task)


@router.get("/{task_id}")
async def get_task(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_scoped_task(task_id, workspace, db)

    result = task_to_dict(task)

    # Include subtasks (also workspace-scoped)
    sub_query = select(Task).where(Task.parent_id == task.id, Task.workspace_id == workspace.id)
    sub_result = await db.execute(sub_query)
    result["subtasks"] = [task_to_dict(s) for s in sub_result.scalars().all()]

    return result


@router.patch("/{task_id}")
async def update_task(
    task_id: str,
    body: TaskUpdate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_scoped_task(task_id, workspace, db)

    old_status = task.status
    if body.title is not None:
        task.title = body.title
    if body.description is not None:
        task.description = body.description
    if body.priority is not None:
        task.priority = body.priority
    if body.reference_answer is not None:
        task.reference_answer = body.reference_answer
    if body.canonical_trajectory is not None:
        task.canonical_trajectory = body.canonical_trajectory
    if body.capability_spec is not None:
        task.capability_spec = body.capability_spec
    if body.status is not None:
        task.status = body.status
        if body.status == TaskStatus.IN_PROGRESS.value and not task.started_at:
            task.started_at = datetime.utcnow()
        if body.status in (TaskStatus.DONE.value, TaskStatus.FAILED.value):
            task.completed_at = datetime.utcnow()

    await db.commit()
    await db.refresh(task)

    if body.status and body.status != old_status:
        await log_event(
            db, "task_status_changed", "user",
            {"old_status": old_status, "new_status": body.status},
            task_id=task.id, workspace_id=workspace.id,
        )

    return task_to_dict(task)


@router.patch("/{task_id}/approve")
async def approve_task(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_scoped_task(task_id, workspace, db)
    if task.status != TaskStatus.AWAITING_APPROVAL.value:
        raise HTTPException(status_code=400, detail="Task not in awaiting_approval status")

    task.status = TaskStatus.DONE.value
    task.completed_at = datetime.utcnow()
    await db.commit()
    await db.refresh(task)
    await log_event(db, "user_approval", "user", {}, task_id=task.id, workspace_id=workspace.id)
    return task_to_dict(task)


@router.patch("/{task_id}/reject")
async def reject_task(
    task_id: str,
    body: dict,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_scoped_task(task_id, workspace, db)
    if task.status != TaskStatus.AWAITING_APPROVAL.value:
        raise HTTPException(status_code=400, detail="Task not in awaiting_approval status")

    task.user_feedback = body.get("feedback", "")
    task.status = TaskStatus.READY.value
    task.agent_container_id = None
    await db.commit()
    await db.refresh(task)
    await log_event(
        db, "user_rejection", "user",
        {"feedback": task.user_feedback, "action": "re-queued to ready"},
        task_id=task.id, workspace_id=workspace.id,
    )
    return task_to_dict(task)


@router.delete("/{task_id}")
async def delete_task(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_scoped_task(task_id, workspace, db)
    if task.status != TaskStatus.BACKLOG.value:
        raise HTTPException(status_code=400, detail="Can only delete tasks in backlog")
    # Delete related events and chat messages first (FK constraints)
    from app.models.event import AgentEvent
    from app.models.chat_message import ChatMessage
    from sqlalchemy import delete
    await db.execute(delete(AgentEvent).where(AgentEvent.task_id == task.id))
    await db.execute(delete(ChatMessage).where(ChatMessage.related_task_id == task.id))
    await db.delete(task)
    await db.commit()
    return {"status": "deleted"}


@router.get("/{task_id}/decomposition", response_model=DecompositionResponse)
async def get_task_decomposition(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Tree + per-attempt timeline for a parent task and its subtasks.

    Used by the Decomposition view (/graph?view=decomposition) to surface the
    structure of a decomposed task plus the actual chronology of every agent
    container that ran (including retries) — far more diagnostic than the
    star-shaped Communication graph for our hub-and-spoke architecture.
    """
    from app.models.event import AgentEvent
    from app.models.template import Template

    parent = await _get_scoped_task(task_id, workspace, db)

    sub_rows = (await db.execute(
        select(Task, Template.name)
        .outerjoin(Template, Task.template_id == Template.id)
        .where(Task.parent_id == parent.id, Task.workspace_id == workspace.id)
        .order_by(Task.created_at)
    )).all()

    sub_ids = [s.id for s, _ in sub_rows]

    events_by_container: dict[str, list[AgentEvent]] = {}
    if sub_ids:
        events = (await db.execute(
            select(AgentEvent)
            .where(
                AgentEvent.task_id.in_(sub_ids),
                AgentEvent.event_type.in_(
                    ("agent_spawned", "agent_completed", "agent_failed", "agent_aborted")
                ),
            )
            .order_by(AgentEvent.created_at)
        )).scalars().all()
        for ev in events:
            cid = ev.agent_container_id
            if not cid:
                continue
            events_by_container.setdefault(cid, []).append(ev)

    OUTCOME_BY_TYPE = {
        "agent_completed": "completed",
        "agent_failed": "failed",
        "agent_aborted": "aborted",
    }

    def _attempts_for(task_id_: uuid.UUID) -> list[dict]:
        result: list[dict] = []
        for cid, evs in events_by_container.items():
            evs = [e for e in evs if e.task_id == task_id_]
            if not evs:
                continue
            spawned = next((e for e in evs if e.event_type == "agent_spawned"), None)
            if not spawned:
                continue
            terminal = next(
                (e for e in reversed(evs) if e.event_type != "agent_spawned"), None
            )
            outcome = OUTCOME_BY_TYPE.get(terminal.event_type, "running") if terminal else "running"
            error = None
            if terminal and outcome in ("failed", "aborted"):
                error = (terminal.data or {}).get("error") or (terminal.data or {}).get("reason")
            result.append({
                "agent_container_id": cid,
                "spawned_at": spawned.created_at.isoformat(),
                "finished_at": terminal.created_at.isoformat() if terminal else None,
                "outcome": outcome,
                "error": error,
            })
        result.sort(key=lambda a: a["spawned_at"])
        return result

    subtasks = []
    for s, template_name in sub_rows:
        subtasks.append({
            "id": str(s.id),
            "title": s.title,
            "template_name": template_name,
            "status": s.status,
            "retry_count": s.retry_count,
            "max_retries": s.max_retries,
            "depends_on": [str(d) for d in (s.depends_on or [])],
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "completed_at": s.completed_at.isoformat() if s.completed_at else None,
            "cost_usd": float(s.cost_usd) if s.cost_usd is not None else 0.0,
            "result_files_count": len(s.result_files or []),
            "attempts": _attempts_for(s.id),
        })

    return {
        "parent": {
            "id": str(parent.id),
            "title": parent.title,
            "status": parent.status,
            "started_at": parent.started_at.isoformat() if parent.started_at else None,
            "completed_at": parent.completed_at.isoformat() if parent.completed_at else None,
            "cost_usd": float(parent.cost_usd) if parent.cost_usd is not None else 0.0,
        },
        "subtasks": subtasks,
    }


@router.get("/{task_id}/files/{file_name:path}")
async def download_task_file(
    task_id: str,
    file_name: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Download a result file from MinIO."""
    # Side-effect: scoping check — raises 404 if task isn't in this workspace.
    await _get_scoped_task(task_id, workspace, db)

    s3_path = f"results/{task_id}/{file_name}"

    try:
        from app.storage.minio_client import get_file_stream
        stream = get_file_stream(s3_path)
        return StreamingResponse(
            stream,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
        )
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")


@router.get("/{task_id}/files.zip")
async def download_task_files_zip(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Stream a ZIP archive containing every result file for this task."""
    import io
    import zipfile

    task = await _get_scoped_task(task_id, workspace, db)
    files = list(task.result_files or [])
    if not files:
        raise HTTPException(status_code=404, detail="No files for this task")

    from app.storage.minio_client import get_file_stream

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for s3_path in files:
            arcname = s3_path.split("/", 2)[-1] if "/" in s3_path else s3_path
            try:
                stream = get_file_stream(s3_path)
                try:
                    zf.writestr(arcname, stream.read())
                finally:
                    stream.close()
                    stream.release_conn()
            except Exception:
                # Skip missing/unreadable files rather than failing the whole archive.
                continue
    buf.seek(0)

    fname = f"task_{task_id[:8]}_files.zip"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
