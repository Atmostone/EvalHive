"""Retired agent webhook paths; execution evidence now belongs to attempts."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import verify_agent_token
from app.database import get_db
from app.models.task import Task

router = APIRouter(tags=["webhooks"])


@router.post("/api/v1/agent-webhook/{task_id}")
@router.post("/api/agent-webhook/{task_id}")
async def retired_webhook(task_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    try:
        identity = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(400, "Invalid task ID") from None
    token = await verify_agent_token(db, plain=authorization[7:], task_id=identity)
    if token is None:
        raise HTTPException(401, "Invalid or expired agent token")
    task = await db.get(Task, identity)
    if task is not None and task.current_attempt_id is not None:
        raise HTTPException(409, "Attempt tasks accept writes only through the runner protocol")
    raise HTTPException(410, "Legacy agent execution was retired; create a new task using an installed harness",
                        headers={"Deprecation": "true", "Link": '</api/harnesses>; rel="successor-version"'})
