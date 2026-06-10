"""Tool & MCP Registry API (SPA-41).

Workspace-scoped CRUD for the user-level registry of tools and MCP servers that
templates reference by id, plus a best-effort connection test. Writes are
owner/admin-only; secrets are masked on every read (only the spawn-time resolver
reveals them).
"""

from __future__ import annotations

import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, get_current_workspace, require_role
from app.database import get_db
from app.models.user import User
from app.models.workspace import Workspace
from app.registry import service

router = APIRouter(prefix="/api/registry", tags=["registry"])


class EntryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: Literal["builtin", "mcp"] = "builtin"
    config: dict = Field(default_factory=dict)
    secrets: dict = Field(default_factory=dict)
    enabled: bool = True
    description: Optional[str] = None


class EntryUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    config: Optional[dict] = None
    secrets: Optional[dict] = None
    enabled: Optional[bool] = None
    description: Optional[str] = None


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid registry entry id")


@router.get("/tools")
async def list_tools(
    kind: Optional[str] = Query(None),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """List registry entries (secrets masked), optionally filtered by ``kind``."""
    entries = await service.list_entries(db, workspace_id=workspace.id, kind=kind)
    return [service.serialize(e) for e in entries]


@router.post("/tools", status_code=status.HTTP_201_CREATED)
async def create_tool(
    body: EntryCreate,
    workspace: Workspace = Depends(get_current_workspace),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Register a new tool or MCP server."""
    try:
        entry = await service.create_entry(
            db,
            workspace_id=workspace.id,
            name=body.name,
            kind=body.kind,
            config=body.config,
            secrets=body.secrets,
            enabled=body.enabled,
            description=body.description,
            created_by=getattr(user, "email", None) or "user",
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return service.serialize(entry)


@router.get("/tools/{entry_id}")
async def get_tool(
    entry_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    entry = await service.get_entry(db, _parse_uuid(entry_id), workspace_id=workspace.id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "registry entry not found")
    return service.serialize(entry)


@router.put("/tools/{entry_id}")
async def update_tool(
    entry_id: str,
    body: EntryUpdate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    try:
        entry = await service.update_entry(
            db,
            _parse_uuid(entry_id),
            workspace_id=workspace.id,
            **body.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        msg = str(e)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND if "not found" in msg else status.HTTP_400_BAD_REQUEST, msg
        )
    return service.serialize(entry)


@router.delete("/tools/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tool(
    entry_id: str,
    force: bool = Query(False),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Delete an entry. 409 if referenced by templates unless ``force=true`` (then
    the reference is stripped from those templates)."""
    try:
        await service.delete_entry(
            db, _parse_uuid(entry_id), workspace_id=workspace.id, force=force
        )
    except service.RegistryConflict as e:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"detail": "registry entry is referenced by templates", "templates": e.referencing},
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e))


@router.post("/tools/{entry_id}/test")
async def test_tool(
    entry_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin")),
):
    """Best-effort connection/validity check (no live spawn for stdio MCP)."""
    entry = await service.get_entry(db, _parse_uuid(entry_id), workspace_id=workspace.id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "registry entry not found")
    return await service.test_entry(entry)
