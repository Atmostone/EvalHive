"""Workspace-scoped configuration endpoints (system model assignment)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_workspace, require_role
from app.database import get_db
from app.models.provider import LLMModel, Provider
from app.models.workspace import Workspace

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


class SystemModelsBody(BaseModel):
    orchestrator_model_id: str | None = None
    chat_model_id: str | None = None
    memory_extractor_model_id: str | None = None
    quality_judge_model_id: str | None = None


def _system_models_dict(ws: Workspace) -> dict:
    return {
        "orchestrator_model_id": str(ws.orchestrator_model_id) if ws.orchestrator_model_id else None,
        "chat_model_id": str(ws.chat_model_id) if ws.chat_model_id else None,
        "memory_extractor_model_id": str(ws.memory_extractor_model_id) if ws.memory_extractor_model_id else None,
        "quality_judge_model_id": str(ws.quality_judge_model_id) if ws.quality_judge_model_id else None,
    }


async def _validate_model_in_workspace(
    model_id_str: str | None, workspace: Workspace, db: AsyncSession
) -> uuid.UUID | None:
    if model_id_str is None:
        return None
    try:
        mid = uuid.UUID(model_id_str)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid model id") from None
    model = await db.get(LLMModel, mid)
    if model is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model not found")
    provider = await db.get(Provider, model.provider_id)
    if provider is None or provider.workspace_id != workspace.id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "model does not belong to this workspace"
        )
    return mid


@router.get("/me/system-models")
async def get_system_models(
    workspace: Workspace = Depends(get_current_workspace),
):
    return _system_models_dict(workspace)


@router.patch("/me/system-models")
async def update_system_models(
    body: SystemModelsBody,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Set or clear (pass null) any of the three system model FKs."""
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return _system_models_dict(workspace)

    for field, value in fields.items():
        mid = await _validate_model_in_workspace(value, workspace, db)
        setattr(workspace, field, mid)

    await db.commit()
    await db.refresh(workspace)
    return _system_models_dict(workspace)
