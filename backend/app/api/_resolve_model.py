"""Helpers to resolve a Provider+LLMModel pair for a workspace/template/system kind.

Always raises HTTP 400 with an explicit message when configuration is missing,
so the failure surfaces to the user instead of falling back to invisible defaults.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.provider import LLMModel, Provider
from app.models.workspace import Workspace
from app.plugins.llm import set_provider_concurrency


SystemModelKind = Literal["orchestrator", "chat", "memory_extractor", "quality_judge"]


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    provider: Provider
    model: LLMModel


_WORKSPACE_FIELD = {
    "orchestrator": "orchestrator_model_id",
    "chat": "chat_model_id",
    "memory_extractor": "memory_extractor_model_id",
    "quality_judge": "quality_judge_model_id",
}


async def _load_pair(db: AsyncSession, model_id: uuid.UUID) -> ResolvedModel:
    model = await db.get(LLMModel, model_id)
    if model is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"LLM model {model_id} not found (was it deleted?)",
        )
    provider = await db.get(Provider, model.provider_id)
    if provider is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Provider for model {model_id} not found",
        )
    # Every LLM use resolves through here — keep the per-provider concurrency
    # registry (SPA-47) in sync without a DB hit on the completion hot path.
    set_provider_concurrency(provider.endpoint, provider.api_key, provider.max_concurrency)
    return ResolvedModel(provider=provider, model=model)


async def resolve_workspace_model(
    db: AsyncSession,
    workspace_id: uuid.UUID | str,
    kind: SystemModelKind,
) -> ResolvedModel:
    """Resolve the system model assigned to ``workspace_id`` for ``kind``."""
    if isinstance(workspace_id, str):
        try:
            workspace_id = uuid.UUID(workspace_id)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"invalid workspace id: {workspace_id}",
            ) from exc

    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")

    field = _WORKSPACE_FIELD[kind]
    model_id = getattr(workspace, field)
    if model_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"workspace has no {kind} model configured — assign one in Settings → System Models",
        )
    return await _load_pair(db, model_id)


async def resolve_model_by_id(
    db: AsyncSession,
    model_id: uuid.UUID | str | None,
) -> ResolvedModel:
    """Resolve a model by its id (e.g. from ``Template.model_id``)."""
    if model_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "template has no model configured — pick one in the template editor",
        )
    if isinstance(model_id, str):
        try:
            model_id = uuid.UUID(model_id)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"invalid model id: {model_id}",
            ) from exc
    return await _load_pair(db, model_id)


def mask_api_key(api_key: str) -> str:
    """Return ``***last4`` masked form for API responses."""
    if not api_key:
        return ""
    if len(api_key) <= 4:
        return "***"
    return f"***{api_key[-4:]}"
