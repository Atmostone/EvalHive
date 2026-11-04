import logging

from evalhive_harness import ProtocolError
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_workspace, require_role
from app.database import get_db
from app.harnesses.attempts import AttemptError, queue_command
from app.models.execution_attempt import ExecutionAttempt
from app.models.task import Task
from app.models.workspace import Workspace
from app.plugins.runtime import get_agent_runtime
from app.utils.events import log_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])


async def _attempt_for_container(db, workspace_id, container_id):
    return await db.scalar(
        select(ExecutionAttempt)
        .join(Task, Task.current_attempt_id == ExecutionAttempt.id)
        .where(
            ExecutionAttempt.container_id == container_id,
            ExecutionAttempt.workspace_id == workspace_id,
            Task.workspace_id == workspace_id,
        )
    )


async def _queue_attempt_control(db, attempt, name, payload):
    try:
        receipt = await queue_command(db, attempt.id, name, payload)
        await db.commit()
        return receipt
    except (AttemptError, ProtocolError) as error:
        await db.rollback()
        status = error.status if isinstance(error, AttemptError) else 422
        raise HTTPException(status, {"code": error.code, "message": str(error)}) from None


@router.get("")
async def get_agents(workspace: Workspace = Depends(get_current_workspace)):
    """List active agent containers in this workspace."""
    return get_agent_runtime().list_active(workspace_id=str(workspace.id))


@router.get("/{container_id}")
async def get_agent(
    container_id: str,
    workspace: Workspace = Depends(get_current_workspace),
):
    stats = get_agent_runtime().stats(container_id, workspace_id=str(workspace.id))
    if not stats:
        raise HTTPException(status_code=404, detail="Container not found")
    return stats


@router.post("/{container_id}/kill", dependencies=[Depends(require_role("owner", "admin"))])
async def kill_agent_endpoint(
    container_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    runtime = get_agent_runtime()
    stats = runtime.stats(container_id, workspace_id=str(workspace.id))
    if not stats:
        raise HTTPException(status_code=404, detail="Container not found")
    task_id = stats.get("task_id")

    success = runtime.kill(container_id, workspace_id=str(workspace.id))
    if not success:
        raise HTTPException(status_code=404, detail="Container not found")

    if task_id:
        await log_event(
            db,
            "agent_killed",
            "user",
            {"container_id": container_id},
            task_id=task_id,
            workspace_id=workspace.id,
        )

    return {"status": "killed"}


@router.post("/kill-all", dependencies=[Depends(require_role("owner", "admin"))])
async def kill_all_endpoint(
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Kill all agent containers in this workspace (kill switch)."""
    count = get_agent_runtime().kill_all(workspace_id=str(workspace.id))
    await log_event(db, "kill_all_agents", "user", {"count": count}, workspace_id=workspace.id)
    return {"status": "ok", "killed": count}


@router.get("/{container_id}/health")
async def agent_health_endpoint(
    container_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    attempt = await _attempt_for_container(db, workspace.id, container_id)
    if attempt is not None:
        return {
            "status": attempt.status,
            "attempt_id": str(attempt.id),
            "last_heartbeat_at": attempt.last_heartbeat_at.isoformat(),
        }
    runtime = get_agent_runtime()
    if not runtime.stats(container_id, workspace_id=str(workspace.id)):
        raise HTTPException(status_code=404, detail="Agent not found")
    health = await runtime.health(container_id)
    if health is None:
        raise HTTPException(status_code=404, detail="Agent unreachable")
    return health


class FeedbackBody(BaseModel):
    message: str


@router.post(
    "/{container_id}/feedback",
    dependencies=[Depends(require_role("owner", "admin", "member"))],
)
async def agent_feedback(
    container_id: str,
    body: FeedbackBody,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message is empty")
    attempt = await _attempt_for_container(db, workspace.id, container_id)
    if attempt is not None:
        receipt = await _queue_attempt_control(db, attempt, "feedback", {"text": body.message})
        return {"status": "queued", "command_id": receipt["id"]}
    runtime = get_agent_runtime()
    stats = runtime.stats(container_id, workspace_id=str(workspace.id))
    if not stats:
        raise HTTPException(status_code=404, detail="Agent not found")
    ok = await runtime.send_command(container_id, "feedback", {"message": body.message})
    if not ok:
        raise HTTPException(status_code=502, detail="Agent did not accept feedback")
    task_id = stats.get("task_id")
    await log_event(
        db,
        "agent_feedback_sent",
        "user",
        {"message": body.message[:500]},
        task_id=task_id,
        agent_container_id=container_id,
        workspace_id=workspace.id,
    )
    return {"status": "queued"}


class SwitchModelBody(BaseModel):
    model_id: str


@router.post(
    "/{container_id}/switch_model",
    dependencies=[Depends(require_role("owner", "admin"))],
)
async def agent_switch_model(
    container_id: str,
    body: SwitchModelBody,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    attempt = await _attempt_for_container(db, workspace.id, container_id)
    if attempt is not None:
        raise HTTPException(
            422,
            {
                "code": "UNSUPPORTED_CAPABILITY",
                "message": "Changing the model during an SDK attempt is unavailable",
            },
        )
    from app.api._resolve_model import resolve_model_by_id

    resolved = await resolve_model_by_id(db, body.model_id)
    provider = resolved.provider
    if provider.workspace_id != workspace.id:
        raise HTTPException(status_code=400, detail="model does not belong to this workspace")

    runtime = get_agent_runtime()
    stats = runtime.stats(container_id, workspace_id=str(workspace.id))
    if not stats:
        raise HTTPException(status_code=404, detail="Agent not found")
    payload = {
        "model": resolved.model.api_name,
        "base_url": provider.endpoint,
        "api_key": provider.api_key,
    }
    ok = await runtime.send_command(container_id, "switch_model", payload)
    if not ok:
        raise HTTPException(status_code=502, detail="Agent did not accept switch_model")
    task_id = stats.get("task_id")
    await log_event(
        db,
        "agent_model_switched",
        "user",
        {
            "model_id": body.model_id,
            "model": resolved.model.api_name,
            "provider": provider.name,
        },
        task_id=task_id,
        agent_container_id=container_id,
        workspace_id=workspace.id,
    )
    return {"status": "queued"}


class AbortBody(BaseModel):
    reason: str = "user requested"


@router.post(
    "/{container_id}/abort",
    dependencies=[Depends(require_role("owner", "admin"))],
)
async def agent_abort(
    container_id: str,
    body: AbortBody,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    attempt = await _attempt_for_container(db, workspace.id, container_id)
    if attempt is not None:
        receipt = await _queue_attempt_control(db, attempt, "abort", {})
        return {"status": "abort_signaled", "command_id": receipt["id"]}
    runtime = get_agent_runtime()
    stats = runtime.stats(container_id, workspace_id=str(workspace.id))
    if not stats:
        raise HTTPException(status_code=404, detail="Agent not found")
    ok = await runtime.send_command(container_id, "abort", {"reason": body.reason})
    if not ok:
        raise HTTPException(status_code=502, detail="Agent unreachable")
    task_id = stats.get("task_id")
    await log_event(
        db,
        "agent_abort_signaled",
        "user",
        {"reason": body.reason},
        task_id=task_id,
        agent_container_id=container_id,
        workspace_id=workspace.id,
    )
    return {"status": "abort_signaled"}
