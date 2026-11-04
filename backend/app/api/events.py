import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_workspace
from app.auth.security import decode_access_token
from app.database import async_session, get_db
from app.models.event import AgentEvent
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.utils.events import (
    register_event_client,
    unregister_event_client,
    update_event_client_filters,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["events"])

# Separate router for WebSocket (no prefix, so path is /ws/events)
ws_router = APIRouter(tags=["events"])


def event_to_dict(e: AgentEvent) -> dict:
    return {
        "id": e.id,
        "task_id": str(e.task_id) if e.task_id else None,
        "agent_container_id": e.agent_container_id,
        "event_type": e.event_type,
        "source": e.source,
        "data": e.data,
        "workspace_id": str(e.workspace_id) if e.workspace_id else None,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


@router.get("")
async def list_events(
    task_id: str | None = None,
    event_type: str | None = None,
    source: str | None = None,
    agent_container_id: str | None = None,
    from_dt: str | None = None,
    to_dt: str | None = None,
    limit: int = 100,
    offset: int = 0,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    query = select(AgentEvent).where(AgentEvent.workspace_id == workspace.id)
    if task_id:
        query = query.where(AgentEvent.task_id == task_id)
    if event_type:
        query = query.where(AgentEvent.event_type == event_type)
    if source:
        query = query.where(AgentEvent.source == source)
    if agent_container_id:
        query = query.where(AgentEvent.agent_container_id == agent_container_id)
    if from_dt:
        try:
            dt = datetime.fromisoformat(from_dt.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(UTC).replace(tzinfo=None)
            query = query.where(AgentEvent.created_at >= dt)
        except ValueError:
            raise HTTPException(status_code=400, detail="from_dt must be ISO-8601") from None
    if to_dt:
        try:
            dt = datetime.fromisoformat(to_dt.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(UTC).replace(tzinfo=None)
            query = query.where(AgentEvent.created_at <= dt)
        except ValueError:
            raise HTTPException(status_code=400, detail="to_dt must be ISO-8601") from None
    query = query.order_by(AgentEvent.created_at.desc()).offset(offset).limit(limit)

    result = await db.execute(query)
    events = result.scalars().all()
    return [event_to_dict(e) for e in events]


@router.get("/export/{task_id}")
async def export_task_events(
    task_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Export full event log for a task as JSON download."""
    query = (
        select(AgentEvent)
        .where(
            AgentEvent.task_id == task_id,
            AgentEvent.workspace_id == workspace.id,
        )
        .order_by(AgentEvent.created_at)
    )
    result = await db.execute(query)
    events = result.scalars().all()

    if not events:
        raise HTTPException(status_code=404, detail="No events found for task")

    data = [event_to_dict(e) for e in events]
    return JSONResponse(
        content=data,
        headers={
            "Content-Disposition": f'attachment; filename="events_{task_id[:8]}.json"',
        },
    )


async def _ws_authenticate(ws: WebSocket) -> tuple[User, Workspace] | None:
    """Authenticate a WebSocket via ?token=<jwt>&workspace_id=<uuid> query params.

    Returns (user, workspace) on success, or None on failure (caller closes the socket).
    """
    token = ws.query_params.get("token")
    workspace_id_str = ws.query_params.get("workspace_id")
    if not token:
        return None
    try:
        payload = decode_access_token(token)
    except Exception:
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    if not workspace_id_str:
        workspace_id_str = payload.get("ws")
    if not workspace_id_str:
        return None
    try:
        ws_uuid = uuid.UUID(workspace_id_str)
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        return None

    async with async_session() as db:
        user = await db.get(User, user_uuid)
        if not user or not user.is_active:
            return None
        membership = (
            await db.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.user_id == user_uuid,
                    WorkspaceMember.workspace_id == ws_uuid,
                )
            )
        ).scalar_one_or_none()
        if not membership:
            return None
        workspace = await db.get(Workspace, ws_uuid)
        if not workspace:
            return None
    return user, workspace


@ws_router.websocket("/ws/events")
async def events_websocket(ws: WebSocket):
    await ws.accept()
    auth = await _ws_authenticate(ws)
    if not auth:
        await ws.close(code=4401)
        return
    _, workspace = auth
    logger.info(f"Events WebSocket connected (ws={workspace.id})")

    await register_event_client(ws, filters={"workspace_id": str(workspace.id)})

    try:
        while True:
            data = await ws.receive_text()
            try:
                filters = json.loads(data)
                # Always enforce workspace_id from auth, regardless of client-sent filter
                filters["workspace_id"] = str(workspace.id)
                await update_event_client_filters(ws, filters)
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        logger.info("Events WebSocket disconnected")
    except Exception as e:
        logger.error(f"Events WebSocket error: {e}")
    finally:
        await unregister_event_client(ws)


@ws_router.websocket("/ws/agents/{container_id}")
async def agent_websocket(ws: WebSocket, container_id: str):
    await ws.accept()
    auth = await _ws_authenticate(ws)
    if not auth:
        await ws.close(code=4401)
        return
    _, workspace = auth

    # Verify the agent belongs to this workspace
    from app.plugins.runtime import get_agent_runtime

    if not get_agent_runtime().stats(container_id, workspace_id=str(workspace.id)):
        await ws.close(code=4404)
        return

    logger.info(f"Per-agent WebSocket connected: {container_id[:12]}")
    await register_event_client(
        ws,
        filters={
            "agent_container_id": container_id,
            "workspace_id": str(workspace.id),
        },
    )
    try:
        while True:
            await ws.receive_text()  # ignore client messages
    except WebSocketDisconnect:
        logger.info(f"Per-agent WebSocket disconnected: {container_id[:12]}")
    except Exception as e:
        logger.error(f"Per-agent WebSocket error: {e}")
    finally:
        await unregister_event_client(ws)
