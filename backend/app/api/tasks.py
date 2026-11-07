import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_workspace, require_role
from app.database import get_db
from app.models.task import Task, TaskPriority, TaskStatus
from app.models.workspace import Workspace
from app.schemas.decomposition import DecompositionResponse
from app.utils.events import log_event
from app.utils.failures import is_contaminated

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class TaskCreate(BaseModel):
    title: str
    description: str | None = None
    priority: str = TaskPriority.MEDIUM.value
    parent_id: str | None = None
    reference_answer: str | None = None
    canonical_trajectory: Any | None = None
    capability_spec: Any | None = None
    template_id: uuid.UUID | None = None
    harness_profile_id: uuid.UUID | None = None
    execution_mode: Literal["direct", "orchestrated"] | None = None


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    reference_answer: str | None = None
    canonical_trajectory: Any | None = None
    capability_spec: Any | None = None
    template_id: uuid.UUID | None = None
    harness_profile_id: uuid.UUID | None = None
    execution_mode: Literal["direct", "orchestrated"] | None = None


class TaskRerun(BaseModel):
    reuse_conditions: bool = True
    harness_profile_id: uuid.UUID | None = None
    execution_mode: Literal["direct", "orchestrated"] | None = None


class TaskOut(BaseModel):
    id: str
    parent_id: str | None
    title: str
    description: str | None
    status: str
    priority: str
    failure_type: str | None
    # Whether that reason describes the CONDITION (survives a re-queue) or just
    # this attempt (cleared by one). Two very different futures for the task.
    condition_contaminated: bool = False
    template_id: str | None
    agent_container_id: str | None
    result_summary: str | None
    result_files: list
    token_usage: dict
    orchestrator_usage: dict = {}
    orchestrator_cost_usd: float = 0.0
    retry_count: int
    max_retries: int
    user_feedback: str | None
    orchestrator_feedback: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None

    class Config:
        from_attributes = True


def task_to_dict(task: Task) -> dict:
    return {
        "id": str(task.id),
        "parent_id": str(task.parent_id) if task.parent_id else None,
        "replay_of_task_id": str(task.replay_of_task_id) if task.replay_of_task_id else None,
        "title": task.title,
        # Why this run stopped measuring the model (SPA-87). It was recorded from
        # the start and readable only in SQL: the experiment surfaces carried it,
        # the task itself did not, so an operator looking at a failed task saw
        # «failed» and nothing else (SPA-113).
        "failure_type": task.failure_type,
        "condition_contaminated": bool(task.condition_contaminated),
        "description": task.description,
        "status": task.status,
        "priority": task.priority,
        "origin": task.origin,
        "template_id": str(task.template_id) if task.template_id else None,
        "harness_profile_id": str(task.harness_profile_id) if task.harness_profile_id else None,
        "execution_mode": task.execution_mode,
        "current_attempt_id": str(task.current_attempt_id) if task.current_attempt_id else None,
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
        "cost_usd": float(task.cost_usd) if task.cost_usd is not None else None,
        # The orchestrator's own spend on this task, reported apart from the
        # agent's so the two are never summed by accident (SPA-111).
        "orchestrator_usage": task.orchestrator_usage or {},
        "orchestrator_cost_usd": float(task.orchestrator_cost_usd or 0),
        "depends_on": [str(d) for d in (task.depends_on or [])],
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


async def _get_scoped_task(task_id: str, workspace: Workspace, db: AsyncSession, *, lock=False) -> Task:
    query = select(Task).where(Task.id == uuid.UUID(task_id), Task.workspace_id == workspace.id)
    if lock:
        query = query.with_for_update()
    task = await db.scalar(query)
    if not task or task.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


async def _selection(db, workspace, values):
    from app.api.templates import _validate_harness_profile_id
    from app.models.template import Template

    if values.get("harness_profile_id"):
        await _validate_harness_profile_id(values["harness_profile_id"], workspace, db)
    if values.get("template_id"):
        template = await db.get(Template, values["template_id"])
        if template is None or template.workspace_id != workspace.id:
            raise HTTPException(422, "Role template is unavailable in this workspace")


async def _require_inactive(db, task):
    from app.benchmarks.task_routing import require_ordinary_task

    require_ordinary_task(task)
    from app.harnesses.attempts import TERMINAL
    from app.models.execution_attempt import ExecutionAttempt

    attempt = await db.get(ExecutionAttempt, task.current_attempt_id) if task.current_attempt_id else None
    if attempt is not None and attempt.status not in TERMINAL:
        raise HTTPException(409, "Stop the current attempt before changing execution")


async def _require_unarchived(db, task):
    from app.models.quality_record import QualityRecord

    if await db.scalar(select(QualityRecord.id).where(QualityRecord.task_id == task.id)):
        raise HTTPException(409, "Archived execution is immutable; create a linked rerun")


@router.get("")
async def list_tasks(
    status: str | None = None,
    parent_id: str | None = None,
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


@router.post("", status_code=201, dependencies=[Depends(require_role("owner", "admin", "member"))])
async def create_task(
    body: TaskCreate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    await _selection(db, workspace, body.model_dump())
    task = Task(
        title=body.title,
        description=body.description,
        priority=body.priority,
        parent_id=uuid.UUID(body.parent_id) if body.parent_id else None,
        reference_answer=body.reference_answer,
        canonical_trajectory=body.canonical_trajectory,
        capability_spec=body.capability_spec,
        template_id=body.template_id,
        harness_profile_id=body.harness_profile_id,
        execution_mode=body.execution_mode,
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
    from app.models.quality_record import QualityRecord

    result["results_archived"] = bool(await db.scalar(select(QualityRecord.id).where(QualityRecord.task_id == task.id)))

    # Include subtasks (also workspace-scoped)
    sub_query = select(Task).where(Task.parent_id == task.id, Task.workspace_id == workspace.id)
    sub_result = await db.execute(sub_query)
    result["subtasks"] = [task_to_dict(s) for s in sub_result.scalars().all()]

    return result


# Statuses that put a failed task back in the queue — the point at which its
# recorded failure stops describing what is about to happen.
_REQUEUE_STATUSES = (TaskStatus.READY.value, TaskStatus.BACKLOG.value)


@router.patch("/{task_id}", dependencies=[Depends(require_role("owner", "admin", "member"))])
async def update_task(
    task_id: str,
    body: TaskUpdate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_scoped_task(task_id, workspace, db, lock=True)

    selected = body.model_dump(exclude_unset=True)
    from app.benchmarks.task_routing import require_ordinary_task

    require_ordinary_task(task)
    if any(key in selected for key in ("title", "description",
                                      "template_id", "harness_profile_id", "execution_mode")) or body.status in _REQUEUE_STATUSES:
        await _require_unarchived(db, task)
    selection = {key: selected[key] for key in ("template_id", "harness_profile_id", "execution_mode") if key in selected}
    if selection or (body.status is not None and body.status != task.status):
        await _require_inactive(db, task)
    await _selection(db, workspace, selection)
    for key, value in selection.items():
        setattr(task, key, value)
    if (task.run_config or {}).get("harness_snapshot"):
        rc = dict(task.run_config)
        if "harness_profile_id" in selection:
            rc.pop("harness_snapshot", None)
            rc["new_harness_conditions"] = True
        if "title" in selected or "description" in selected:
            rc["replay_input_override"] = True
        rc["replay_overrides"] = sorted(set(rc.get("replay_overrides", [])) | set(selection))
        task.run_config = rc

    old_status = task.status
    # Evaluation annotations may be added to historical results. They do not
    # alter the frozen execution input, journal or archived result blob.
    evaluation_changes = {
        name: {"before": getattr(task, name), "after": selected[name]}
        for name in ("reference_answer", "canonical_trajectory", "capability_spec")
        if name in selected and selected[name] is not None and selected[name] != getattr(task, name)
    }
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
        # Re-queueing by hand starts a NEW attempt, and `failure_type` describes the
        # attempt that failed — carried across, it marks the next run with a reason
        # that is no longer true, and a contaminating one silently removes a
        # successful measurement from every aggregate (SPA-113).
        #
        # The one SPA-87 exception survives: a contamination that describes the
        # CONDITION rather than the attempt — the orchestrator's LLM died, it fell
        # back to a substituted template and pinned it, so the next attempt runs
        # under the degraded condition too and is not a measurement of the
        # intended treatment either.
        #
        # SPA-115: that used to be inferred from `template_id is not None`, which
        # is true of every orchestrated task. An ordinary run that chose a
        # template and then died on an agent-side 429 therefore kept its reason
        # through the re-queue, and its next — successful — run was dropped from
        # every aggregate by `measures_the_model()`. Only the degrade path knows
        # which kind it is, so only the degrade path records it.
        if old_status == TaskStatus.FAILED.value and body.status in _REQUEUE_STATUSES:
            if not (is_contaminated(task.failure_type) and task.condition_contaminated):
                task.failure_type = None

    await db.commit()
    await db.refresh(task)

    if evaluation_changes:
        await log_event(db, "evaluation_spec_changed", "user", {"changes": evaluation_changes},
                        task_id=task.id, workspace_id=workspace.id)
    if body.status and body.status != old_status:
        await log_event(
            db, "task_status_changed", "user",
            {"old_status": old_status, "new_status": body.status},
            task_id=task.id, workspace_id=workspace.id,
        )

    return task_to_dict(task)


@router.post("/{task_id}/start", dependencies=[Depends(require_role("owner", "admin", "member"))])
async def start_task(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    from evalhive_harness import ProtocolError

    from app.harnesses.catalog import CatalogError
    from app.harnesses.launch import execution_mode, resolve_launch, selected_template
    from app.harnesses.profiles import ProfileError

    task = await _get_scoped_task(task_id, workspace, db, lock=True)
    await _require_inactive(db, task)
    await _require_unarchived(db, task)
    if task.status not in {"backlog", "ready", "failed", "done", "awaiting_approval"}:
        raise HTTPException(409, "Task cannot be started in its current state")
    try:
        template = await selected_template(db, task)
        if execution_mode(task, template) == "direct" or task.harness_profile_id or getattr(template, "harness_profile_id", None):
            await resolve_launch(db, task, template)
    except (ProfileError, CatalogError, ProtocolError) as error:
        raise HTTPException(422, str(error)) from None
    if not (is_contaminated(task.failure_type) and task.condition_contaminated):
        task.failure_type = None
    task.status = TaskStatus.READY.value
    task.completed_at = None
    await db.commit()
    await db.refresh(task)
    await log_event(db, "task_status_changed", "user", {"new_status": "ready"}, task_id=task.id, workspace_id=workspace.id)
    return task_to_dict(task)


@router.post("/{task_id}/rerun", status_code=201, dependencies=[Depends(require_role("owner", "admin", "member"))])
async def rerun_task(
    task_id: str,
    body: TaskRerun,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    from app.harnesses.launch import resolve_launch
    from app.models.execution_attempt import ExecutionAttempt
    from app.orchestrator.rerun import clone_task_for_rerun

    source = await _get_scoped_task(task_id, workspace, db, lock=True)
    if body.reuse_conditions and body.harness_profile_id:
        raise HTTPException(422, "Choose new conditions when selecting a harness profile")
    await _require_inactive(db, source)
    if source.status not in {"done", "failed", "awaiting_approval"}:
        raise HTTPException(409, "Only a settled task can be rerun")
    await _selection(db, workspace, body.model_dump())
    config = {}
    if body.reuse_conditions:
        attempt = await db.get(ExecutionAttempt, source.current_attempt_id) if source.current_attempt_id else None
        if attempt is None or attempt.connector_id.startswith("legacy"):
            raise HTTPException(409, "Historical harness replay is unavailable; create a new run with current settings")
    else:
        config["new_harness_conditions"] = True
    if body.harness_profile_id:
        config["harness_profile_id"] = str(body.harness_profile_id)
    if body.execution_mode:
        config["execution_mode"] = body.execution_mode
    try:
        clone = await clone_task_for_rerun(db, source, run_config=config,
            status="backlog", title_suffix=" (rerun)", commit=False)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    if body.reuse_conditions:
        try:
            await resolve_launch(db, clone)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
    await db.commit()
    await db.refresh(clone)
    return task_to_dict(clone)


@router.patch("/{task_id}/approve", dependencies=[Depends(require_role("owner", "admin", "member"))])
async def approve_task(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_scoped_task(task_id, workspace, db)
    from app.benchmarks.task_routing import require_ordinary_task

    require_ordinary_task(task)
    if task.status != TaskStatus.AWAITING_APPROVAL.value:
        raise HTTPException(status_code=400, detail="Task not in awaiting_approval status")

    task.status = TaskStatus.DONE.value
    task.completed_at = datetime.utcnow()
    await db.commit()
    await db.refresh(task)
    await log_event(db, "user_approval", "user", {}, task_id=task.id, workspace_id=workspace.id)
    return task_to_dict(task)


@router.patch("/{task_id}/reject", dependencies=[Depends(require_role("owner", "admin", "member"))])
async def reject_task(
    task_id: str,
    body: dict,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_scoped_task(task_id, workspace, db, lock=True)
    from app.benchmarks.task_routing import require_ordinary_task

    require_ordinary_task(task)
    if task.status != TaskStatus.AWAITING_APPROVAL.value:
        raise HTTPException(status_code=400, detail="Task not in awaiting_approval status")

    feedback = body.get("feedback", "")
    if not isinstance(feedback, str) or len(feedback) > 65536:
        raise HTTPException(422, "Feedback must be text within the supported size limit")
    from app.models.quality_record import QualityRecord

    record = await db.scalar(select(QualityRecord).where(QualityRecord.task_id == task.id))
    if record is not None:
        from app.orchestrator.rerun import clone_task_for_rerun

        clone = await clone_task_for_rerun(db, task,
            run_config={"replay_input_override": True, "new_harness_conditions": not bool(task.current_attempt_id)},
            status="ready", max_retries=task.max_retries, commit=False)
        clone.user_feedback = feedback
        task.user_feedback, task.status = feedback, TaskStatus.FAILED.value
        record.final_status = task.status
        await db.commit()
        await db.refresh(clone)
        await log_event(db, "user_rejection", "user", {"feedback": feedback, "followup_task_id": str(clone.id)},
                        task_id=task.id, workspace_id=workspace.id)
        return task_to_dict(clone)
    task.user_feedback = feedback
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


@router.delete("/{task_id}", dependencies=[Depends(require_role("owner", "admin", "member"))])
async def delete_task(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    task = await _get_scoped_task(task_id, workspace, db)
    from app.benchmarks.task_routing import require_ordinary_task

    require_ordinary_task(task)
    if task.status != TaskStatus.BACKLOG.value:
        raise HTTPException(status_code=400, detail="Can only delete tasks in backlog")
    # Delete related events and chat messages first (FK constraints)
    from sqlalchemy import delete

    from app.models.chat_message import ChatMessage
    from app.models.event import AgentEvent
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
            "cost_usd": float(s.cost_usd) if s.cost_usd is not None else None,
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
            "cost_usd": float(parent.cost_usd) if parent.cost_usd is not None else None,
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
        from pathlib import PurePosixPath
        from urllib.parse import quote

        from app.storage.minio_client import get_file_stream
        stream = get_file_stream(s3_path)
        def content():
            try:
                yield from stream.stream(amt=65536)
            finally:
                stream.close()
                stream.release_conn()

        return StreamingResponse(
            content(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(PurePosixPath(file_name).name, safe="")},
        )
    except Exception:
        raise HTTPException(status_code=404, detail="File not found") from None


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
            prefix = f"results/{task.id}/attempts/{task.current_attempt_id}/"
            arcname = s3_path[len(prefix):] if s3_path.startswith(prefix) else s3_path.split("/", 2)[-1]
            try:
                stream = get_file_stream(s3_path)
                try:
                    zf.writestr(arcname, stream.read())
                finally:
                    stream.close()
                    stream.release_conn()
            except Exception:
                raise HTTPException(502, "Unable to download all task files") from None
    buf.seek(0)

    fname = f"task_{task_id[:8]}_files.zip"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
