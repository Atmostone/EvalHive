"""CRUD for Providers and LLMModels, plus a model test endpoint."""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._resolve_model import mask_api_key
from app.auth.dependencies import get_current_workspace, require_role
from app.database import get_db
from app.models.provider import LLMModel, Provider
from app.models.workspace import Workspace
from app.plugins.llm import set_provider_concurrency
from app.plugins.llm import get_llm_provider


router = APIRouter(prefix="/api/providers", tags=["providers"])
models_router = APIRouter(prefix="/api/models", tags=["providers"])


# ---------------------------- Schemas ----------------------------


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    api_key: str = Field(min_length=1, max_length=500)
    endpoint: str = Field(min_length=1, max_length=500)
    max_concurrency: Optional[int] = Field(default=None, ge=1, le=1000)


class ProviderUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    api_key: Optional[str] = Field(default=None, min_length=1, max_length=500)
    endpoint: Optional[str] = Field(default=None, min_length=1, max_length=500)
    # 0 clears the limit (NULL → unbounded); omitted leaves it unchanged.
    max_concurrency: Optional[int] = Field(default=None, ge=0, le=1000)


class ModelCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)
    api_name: str = Field(min_length=1, max_length=255)
    input_price_per_1m_usd: Decimal = Decimal("0")
    output_price_per_1m_usd: Decimal = Decimal("0")


class ModelUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    api_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    input_price_per_1m_usd: Optional[Decimal] = None
    output_price_per_1m_usd: Optional[Decimal] = None


# ---------------------------- Serializers ----------------------------


def provider_to_dict(p: Provider) -> dict:
    return {
        "id": str(p.id),
        "name": p.name,
        "endpoint": p.endpoint,
        "max_concurrency": p.max_concurrency,
        "api_key_masked": mask_api_key(p.api_key),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def model_to_dict(m: LLMModel) -> dict:
    return {
        "id": str(m.id),
        "provider_id": str(m.provider_id),
        "display_name": m.display_name,
        "api_name": m.api_name,
        "input_price_per_1m_usd": float(m.input_price_per_1m_usd),
        "output_price_per_1m_usd": float(m.output_price_per_1m_usd),
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


async def _scoped_provider(
    provider_id: str, workspace: Workspace, db: AsyncSession
) -> Provider:
    try:
        pid = uuid.UUID(provider_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid provider id")
    p = await db.get(Provider, pid)
    if not p or p.workspace_id != workspace.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "provider not found")
    return p


async def _scoped_model(
    model_id: str, workspace: Workspace, db: AsyncSession
) -> LLMModel:
    try:
        mid = uuid.UUID(model_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid model id")
    m = await db.get(LLMModel, mid)
    if not m:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model not found")
    provider = await db.get(Provider, m.provider_id)
    if not provider or provider.workspace_id != workspace.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model not found")
    return m


# ---------------------------- Provider routes ----------------------------


@router.get("")
async def list_providers(
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(Provider).where(Provider.workspace_id == workspace.id).order_by(Provider.name)
        )
    ).scalars().all()
    return [provider_to_dict(p) for p in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_provider(
    body: ProviderCreate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    p = Provider(
        workspace_id=workspace.id,
        name=body.name,
        api_key=body.api_key,
        endpoint=body.endpoint,
        max_concurrency=body.max_concurrency,
    )
    db.add(p)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"provider named '{body.name}' already exists"
        )
    await db.refresh(p)
    set_provider_concurrency(p.endpoint, p.api_key, p.max_concurrency)
    return provider_to_dict(p)


@router.patch("/{provider_id}")
async def update_provider(
    provider_id: str,
    body: ProviderUpdate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    p = await _scoped_provider(provider_id, workspace, db)
    if body.name is not None:
        p.name = body.name
    if body.endpoint is not None:
        p.endpoint = body.endpoint
    if body.api_key is not None:
        p.api_key = body.api_key
    if body.max_concurrency is not None:
        p.max_concurrency = body.max_concurrency or None  # 0 → unbounded
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "provider name conflicts with existing")
    await db.refresh(p)
    set_provider_concurrency(p.endpoint, p.api_key, p.max_concurrency)
    return provider_to_dict(p)


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    p = await _scoped_provider(provider_id, workspace, db)
    await db.delete(p)
    await db.commit()
    return None


# ---------------------------- Model routes ----------------------------


@router.get("/{provider_id}/models")
async def list_models(
    provider_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    p = await _scoped_provider(provider_id, workspace, db)
    rows = (
        await db.execute(
            select(LLMModel).where(LLMModel.provider_id == p.id).order_by(LLMModel.display_name)
        )
    ).scalars().all()
    return [model_to_dict(m) for m in rows]


@router.post("/{provider_id}/models", status_code=status.HTTP_201_CREATED)
async def create_model(
    provider_id: str,
    body: ModelCreate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    p = await _scoped_provider(provider_id, workspace, db)
    m = LLMModel(
        provider_id=p.id,
        display_name=body.display_name,
        api_name=body.api_name,
        input_price_per_1m_usd=body.input_price_per_1m_usd,
        output_price_per_1m_usd=body.output_price_per_1m_usd,
    )
    db.add(m)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"model '{body.api_name}' already exists for this provider",
        )
    await db.refresh(m)
    return model_to_dict(m)


@models_router.patch("/{model_id}")
async def update_model(
    model_id: str,
    body: ModelUpdate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    m = await _scoped_model(model_id, workspace, db)
    if body.display_name is not None:
        m.display_name = body.display_name
    if body.api_name is not None:
        m.api_name = body.api_name
    if body.input_price_per_1m_usd is not None:
        m.input_price_per_1m_usd = body.input_price_per_1m_usd
    if body.output_price_per_1m_usd is not None:
        m.output_price_per_1m_usd = body.output_price_per_1m_usd
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "api_name conflicts with existing model")
    await db.refresh(m)
    return model_to_dict(m)


@models_router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(
    model_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    m = await _scoped_model(model_id, workspace, db)
    await db.delete(m)
    await db.commit()
    return None


@models_router.post("/{model_id}/test")
async def test_model(
    model_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Probe configured LLM endpoint with a tiny completion."""
    m = await _scoped_model(model_id, workspace, db)
    provider = await db.get(Provider, m.provider_id)
    if not provider:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "provider missing")

    started = time.perf_counter()
    try:
        resp = await get_llm_provider().acompletion(
            model=m.api_name,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
            api_base=provider.endpoint,
            api_key=provider.api_key,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        sample = (resp.choices[0].message.content or "")[:80]
        return {
            "status": "ok",
            "latency_ms": latency_ms,
            "model": m.api_name,
            "sample": sample,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)[:300]}
