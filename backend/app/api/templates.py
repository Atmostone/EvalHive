import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_workspace, require_role
from app.database import get_db
from app.models.provider import LLMModel, Provider
from app.models.registry_entry import RegistryEntry
from app.models.rubric import Rubric
from app.models.template import Template
from app.models.template_version import TemplateVersion
from app.models.workspace import Workspace

router = APIRouter(prefix="/api/templates", tags=["templates"])


class TemplateCreate(BaseModel):
    name: str
    description: str
    soul_md: str
    model_id: Optional[str] = None
    rubric_id: Optional[str] = None
    # Registry references (SPA-41) — registry entry ids the agent enables.
    tool_ids: list = []
    max_ram: str = "2g"
    max_cpu: int = 100000
    timeout_minutes: int = 60
    tags: list = []


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    soul_md: Optional[str] = None
    model_id: Optional[str] = None
    rubric_id: Optional[str] = None
    tool_ids: Optional[list] = None
    max_ram: Optional[str] = None
    max_cpu: Optional[int] = None
    timeout_minutes: Optional[int] = None
    tags: Optional[list] = None


async def _model_with_provider(
    db: AsyncSession, model_id: Optional[uuid.UUID], workspace_id: uuid.UUID
) -> tuple[Optional[LLMModel], Optional[Provider]]:
    """Return (model, provider) if model belongs to this workspace, else (None, None)."""
    if model_id is None:
        return None, None
    model = await db.get(LLMModel, model_id)
    if not model:
        return None, None
    provider = await db.get(Provider, model.provider_id)
    if not provider or provider.workspace_id != workspace_id:
        return None, None
    return model, provider


def template_to_dict(
    t: Template,
    *,
    model: Optional[LLMModel] = None,
    provider: Optional[Provider] = None,
) -> dict:
    return {
        "id": str(t.id),
        "name": t.name,
        "description": t.description,
        "soul_md": t.soul_md,
        "model_id": str(t.model_id) if t.model_id else None,
        "model_display_name": model.display_name if model else None,
        "model_api_name": model.api_name if model else None,
        "provider_name": provider.name if provider else None,
        "rubric_id": str(t.rubric_id) if t.rubric_id else None,
        "tool_ids": t.tool_ids or [],
        "max_ram": t.max_ram,
        "max_cpu": t.max_cpu,
        "timeout_minutes": t.timeout_minutes,
        "tags": t.tags,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


async def _get_scoped_template(
    template_id: str, workspace: Workspace, db: AsyncSession
) -> Template:
    template = await db.get(Template, uuid.UUID(template_id))
    if not template or template.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


async def _validate_model_id(
    model_id: Optional[str], workspace: Workspace, db: AsyncSession
) -> Optional[uuid.UUID]:
    if model_id is None:
        return None
    try:
        mid = uuid.UUID(model_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid model_id")
    model = await db.get(LLMModel, mid)
    if not model:
        raise HTTPException(status_code=400, detail="model_id not found")
    provider = await db.get(Provider, model.provider_id)
    if not provider or provider.workspace_id != workspace.id:
        raise HTTPException(
            status_code=400, detail="model does not belong to this workspace"
        )
    return mid


async def _validate_rubric_id(
    rubric_id: Optional[str], workspace: Workspace, db: AsyncSession
) -> Optional[uuid.UUID]:
    if rubric_id is None:
        return None
    try:
        rid = uuid.UUID(rubric_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid rubric_id")
    rubric = await db.get(Rubric, rid)
    if not rubric or rubric.workspace_id != workspace.id:
        raise HTTPException(
            status_code=400, detail="rubric does not belong to this workspace"
        )
    return rid


async def _validate_tool_ids(
    tool_ids: Optional[list], workspace: Workspace, db: AsyncSession
) -> list:
    """Validate that every id references a registry entry in this workspace (SPA-41).
    Returns the normalized list of id strings."""
    if not tool_ids:
        return []
    out: list = []
    for raw in tool_ids:
        try:
            tid = uuid.UUID(str(raw))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid tool id: {raw}")
        entry = await db.get(RegistryEntry, tid)
        if entry is None or entry.workspace_id != workspace.id:
            raise HTTPException(status_code=400, detail=f"tool id not in registry: {raw}")
        out.append(str(tid))
    return out


@router.get("")
async def list_templates(
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Template).where(Template.workspace_id == workspace.id).order_by(Template.name)
    )
    templates = result.scalars().all()
    output = []
    for t in templates:
        model, provider = await _model_with_provider(db, t.model_id, workspace.id)
        output.append(template_to_dict(t, model=model, provider=provider))
    return output


@router.post(
    "",
    status_code=201,
    dependencies=[Depends(require_role("owner", "admin", "member"))],
)
async def create_template(
    body: TemplateCreate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    model_uuid = await _validate_model_id(body.model_id, workspace, db)
    rubric_uuid = await _validate_rubric_id(body.rubric_id, workspace, db)
    tool_ids = await _validate_tool_ids(body.tool_ids, workspace, db)
    template = Template(
        name=body.name,
        description=body.description,
        soul_md=body.soul_md,
        model_id=model_uuid,
        rubric_id=rubric_uuid,
        tool_ids=tool_ids,
        max_ram=body.max_ram,
        max_cpu=body.max_cpu,
        timeout_minutes=body.timeout_minutes,
        tags=body.tags,
        workspace_id=workspace.id,
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    model, provider = await _model_with_provider(db, template.model_id, workspace.id)
    return template_to_dict(template, model=model, provider=provider)


@router.get("/{template_id}")
async def get_template(
    template_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    t = await _get_scoped_template(template_id, workspace, db)
    model, provider = await _model_with_provider(db, t.model_id, workspace.id)
    return template_to_dict(t, model=model, provider=provider)


def _full_template_snapshot(t: Template) -> dict:
    """Snapshot used for versioning."""
    return {
        "name": t.name,
        "description": t.description,
        "soul_md": t.soul_md,
        "model_id": str(t.model_id) if t.model_id else None,
        "rubric_id": str(t.rubric_id) if t.rubric_id else None,
        "tool_ids": t.tool_ids or [],
        "max_ram": t.max_ram,
        "max_cpu": t.max_cpu,
        "timeout_minutes": t.timeout_minutes,
        "tags": t.tags,
    }


async def _next_version(db: AsyncSession, template_id: uuid.UUID) -> int:
    cur = await db.scalar(
        select(func.max(TemplateVersion.version)).where(
            TemplateVersion.template_id == template_id
        )
    )
    return int(cur or 0) + 1


@router.put(
    "/{template_id}",
    dependencies=[Depends(require_role("owner", "admin", "member"))],
)
async def update_template(
    template_id: str,
    body: TemplateUpdate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    template = await _get_scoped_template(template_id, workspace, db)

    db.add(TemplateVersion(
        template_id=template.id,
        version=await _next_version(db, template.id),
        snapshot=_full_template_snapshot(template),
        tool_ids=template.tool_ids,
        commit_message="auto: pre-update snapshot",
        workspace_id=workspace.id,
    ))

    payload = body.model_dump(exclude_unset=True)
    if "model_id" in payload:
        payload["model_id"] = await _validate_model_id(payload["model_id"], workspace, db)
    if "rubric_id" in payload:
        payload["rubric_id"] = await _validate_rubric_id(payload["rubric_id"], workspace, db)
    if "tool_ids" in payload:
        payload["tool_ids"] = await _validate_tool_ids(payload["tool_ids"], workspace, db)
    for field, value in payload.items():
        setattr(template, field, value)

    await db.commit()
    await db.refresh(template)
    model, provider = await _model_with_provider(db, template.model_id, workspace.id)
    return template_to_dict(template, model=model, provider=provider)


@router.get("/{template_id}/versions")
async def list_versions(
    template_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    await _get_scoped_template(template_id, workspace, db)
    rows = (
        await db.execute(
            select(TemplateVersion)
            .where(TemplateVersion.template_id == uuid.UUID(template_id))
            .order_by(TemplateVersion.version.desc())
        )
    ).scalars().all()
    return [
        {
            "id": str(v.id),
            "version": v.version,
            "commit_message": v.commit_message,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "created_by": v.created_by,
        }
        for v in rows
    ]


@router.get("/{template_id}/versions/{version}")
async def get_version(
    template_id: str,
    version: int,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    await _get_scoped_template(template_id, workspace, db)
    row = (
        await db.execute(
            select(TemplateVersion).where(
                TemplateVersion.template_id == uuid.UUID(template_id),
                TemplateVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Version not found")
    return {
        "id": str(row.id),
        "version": row.version,
        "snapshot": row.snapshot,
        "commit_message": row.commit_message,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# Fields we accept when rolling back. Legacy keys (`model`, `provider_url`,
# `provider_api_key`) are silently dropped to keep old snapshots usable.
_ROLLBACK_FIELDS = {
    "name", "description", "soul_md", "model_id", "rubric_id",
    "tool_ids", "max_ram", "max_cpu", "timeout_minutes", "tags",
}


async def _rollback_model_id_from_legacy(
    snap: dict, workspace_id: uuid.UUID, db: AsyncSession
) -> Optional[uuid.UUID]:
    """Best-effort map an old snapshot's `model` string to a current model id."""
    legacy = snap.get("model")
    if not legacy:
        return None
    row = (
        await db.execute(
            select(LLMModel.id)
            .join(Provider, Provider.id == LLMModel.provider_id)
            .where(
                Provider.workspace_id == workspace_id,
                LLMModel.api_name == legacy,
            )
        )
    ).scalar_one_or_none()
    return row


@router.post(
    "/{template_id}/rollback/{version}",
    dependencies=[Depends(require_role("owner", "admin"))],
)
async def rollback_template(
    template_id: str,
    version: int,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    template = await _get_scoped_template(template_id, workspace, db)
    src = (
        await db.execute(
            select(TemplateVersion).where(
                TemplateVersion.template_id == template.id,
                TemplateVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if not src:
        raise HTTPException(status_code=404, detail="Version not found")

    # Snapshot current before rollback so we can roll back the rollback.
    db.add(TemplateVersion(
        template_id=template.id,
        version=await _next_version(db, template.id),
        snapshot=_full_template_snapshot(template),
        tool_ids=template.tool_ids,
        commit_message=f"auto: pre-rollback to v{version}",
        workspace_id=workspace.id,
    ))

    snap = src.snapshot or {}
    # Resolve legacy `model` (string) to model_id if snap is from before the providers feature.
    if "model_id" not in snap and snap.get("model"):
        mapped = await _rollback_model_id_from_legacy(snap, workspace.id, db)
        snap = {**snap, "model_id": str(mapped) if mapped else None}

    for field, value in snap.items():
        if field not in _ROLLBACK_FIELDS:
            continue
        if field in ("model_id", "rubric_id"):
            if isinstance(value, str):
                try:
                    value = uuid.UUID(value)
                except ValueError:
                    value = None
        setattr(template, field, value)

    db.add(TemplateVersion(
        template_id=template.id,
        version=await _next_version(db, template.id),
        snapshot=_full_template_snapshot(template),
        tool_ids=template.tool_ids,
        commit_message=f"rollback to v{version}",
        workspace_id=workspace.id,
    ))

    await db.commit()
    await db.refresh(template)
    model, provider = await _model_with_provider(db, template.model_id, workspace.id)
    return template_to_dict(template, model=model, provider=provider)


@router.delete(
    "/{template_id}",
    dependencies=[Depends(require_role("owner", "admin"))],
)
async def delete_template(
    template_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    template = await _get_scoped_template(template_id, workspace, db)
    await db.delete(template)
    await db.commit()
    return {"status": "deleted"}
