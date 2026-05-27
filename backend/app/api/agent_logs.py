"""Agent terminal log streaming — ingest + paginated read + WS live + MinIO archive."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.events import _ws_authenticate
from app.auth.dependencies import get_current_workspace
from app.auth.tokens import verify_agent_token
from app.database import get_db
from app.models.agent_log import AgentLogChunk, AgentLogDelivery
from app.models.task import Task
from app.models.workspace import Workspace
from app.schemas.agent_log import AgentLogChunkIn
from app.utils.events import (
    broadcast_log_chunk,
    register_event_client,
    unregister_event_client,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent-logs"])
ws_router = APIRouter(tags=["agent-logs"])


def _chunk_to_dict(c: AgentLogChunk) -> dict:
    return {
        "id": str(c.id),
        "task_id": str(c.task_id),
        "workspace_id": str(c.workspace_id),
        "chunk_seq": c.chunk_seq,
        "content": c.content,
        "tool_name": c.tool_name,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.post("/api/v1/agent-log/{task_id}")
async def ingest_log_chunk(
    task_id: str,
    body: AgentLogChunkIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    plain = auth[7:]

    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid task id")

    token_row = await verify_agent_token(db, plain=plain, task_id=task_uuid)
    if token_row is None:
        raise HTTPException(status_code=401, detail="invalid or expired agent token")

    task = await db.get(Task, task_uuid)
    if task is None:
        return {"status": "error", "detail": "Task not found"}

    existing_delivery = await db.execute(
        select(AgentLogDelivery).where(
            AgentLogDelivery.task_id == task.id,
            AgentLogDelivery.idempotency_key == body.idempotency_key,
        )
    )
    if existing_delivery.scalar_one_or_none() is not None:
        return {"status": "duplicate"}

    max_seq_row = await db.execute(
        select(func.max(AgentLogChunk.chunk_seq)).where(
            AgentLogChunk.task_id == task.id
        )
    )
    existing_max = max_seq_row.scalar()
    chunk_seq = body.chunk_seq
    if existing_max is not None and chunk_seq <= existing_max:
        chunk_seq = existing_max + 1

    db.add(AgentLogDelivery(task_id=task.id, idempotency_key=body.idempotency_key))
    chunk = AgentLogChunk(
        task_id=task.id,
        workspace_id=task.workspace_id,
        chunk_seq=chunk_seq,
        content=body.content,
        tool_name=body.tool_name,
    )
    db.add(chunk)
    try:
        await db.commit()
        await db.refresh(chunk)
    except IntegrityError:
        await db.rollback()
        return {"status": "duplicate"}

    try:
        await broadcast_log_chunk(_chunk_to_dict(chunk))
    except Exception as e:
        logger.warning(f"log chunk broadcast failed: {e}")

    return {"status": "ok", "chunk_seq": chunk_seq}


@router.get("/api/tasks/{task_id}/log")
async def list_log_chunks(
    task_id: str,
    from_seq: int = 0,
    limit: int = 200,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Paginated read of an agent's full terminal log.

    If `tasks.log_archive_s3_path` is set (post-compaction), read from MinIO blob.
    Otherwise, query `agent_log_chunks`. The frontend uses the `archived` flag
    to render a badge but otherwise treats both sources uniformly.
    """
    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid task id")

    task = await db.get(Task, task_uuid)
    if task is None or task.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="task not found")

    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be 1..1000")

    if task.log_archive_s3_path:
        try:
            from app.storage.minio_client import decode_log_archive, read_log_archive

            blob = read_log_archive(task.log_archive_s3_path).decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"reading log archive {task.log_archive_s3_path} failed: {e}")
            blob = ""
        # Decode per-chunk (JSON-lines preserves tool_name; legacy format → None).
        decoded = decode_log_archive(blob)
        sliced = decoded[from_seq : from_seq + limit]
        return {
            "archived": True,
            "archive_path": task.log_archive_s3_path,
            "chunks": [
                {"id": None, "chunk_seq": from_seq + i, "content": d["content"],
                 "tool_name": d.get("tool_name"), "created_at": None}
                for i, d in enumerate(sliced)
            ],
        }

    result = await db.execute(
        select(AgentLogChunk)
        .where(
            AgentLogChunk.task_id == task.id,
            AgentLogChunk.chunk_seq >= from_seq,
        )
        .order_by(AgentLogChunk.chunk_seq)
        .limit(limit)
    )
    chunks = result.scalars().all()

    return {
        "archived": False,
        "archive_path": None,
        "chunks": [
            {
                "id": str(c.id),
                "chunk_seq": c.chunk_seq,
                "content": c.content,
                "tool_name": c.tool_name,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in chunks
        ],
    }


@ws_router.websocket("/ws/tasks/{task_id}/log")
async def task_log_websocket(ws: WebSocket, task_id: str):
    await ws.accept()
    auth = await _ws_authenticate(ws)
    if not auth:
        await ws.close(code=4401)
        return
    _, workspace = auth

    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError:
        await ws.close(code=4400)
        return

    from app.database import async_session

    async with async_session() as db:
        task = await db.get(Task, task_uuid)
        if task is None or task.workspace_id != workspace.id:
            await ws.close(code=4404)
            return

    logger.info(f"Task log WebSocket connected: {task_id[:8]}")
    await register_event_client(
        ws,
        filters={
            "task_id": str(task_uuid),
            "workspace_id": str(workspace.id),
            "_kind": "log_chunk",
        },
    )
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        logger.info(f"Task log WebSocket disconnected: {task_id[:8]}")
    except Exception as e:
        logger.error(f"Task log WebSocket error: {e}")
    finally:
        await unregister_event_client(ws)
