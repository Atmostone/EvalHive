import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.security import create_access_token, hash_password, verify_password
from app.config import get_settings
from app.database import get_db
from app.models.provider import LLMModel, Provider
from app.models.rubric import Rubric
from app.models.template import Template
from app.models.user import User
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace, WorkspaceMember


router = APIRouter(prefix="/api/auth", tags=["auth"])


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slugify(s: str) -> str:
    return _SLUG_RE.sub("-", s.lower()).strip("-") or "ws"


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=200)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    role: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
    default_workspace_id: uuid.UUID


@router.post("/register", response_model=TokenOut)
async def register(body: RegisterIn, db: AsyncSession = Depends(get_db)) -> TokenOut:
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")

    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        display_name=body.display_name or body.email.split("@")[0],
    )
    db.add(user)
    await db.flush()

    base_slug = _slugify(user.display_name or "ws")
    slug = base_slug
    n = 1
    while True:
        clash = await db.execute(select(Workspace).where(Workspace.slug == slug))
        if not clash.scalar_one_or_none():
            break
        n += 1
        slug = f"{base_slug}-{n}"

    workspace = Workspace(name=user.display_name or "Personal", slug=slug, created_by=user.id)
    db.add(workspace)
    await db.flush()

    db.add(WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role="owner"))

    # Clone providers + models from the default workspace so the new user has
    # a working LLM config out of the box. Templates' model_id is then mapped
    # to the cloned model row via (provider.name, model.api_name).
    default_providers = (
        await db.execute(
            select(Provider).where(Provider.workspace_id == DEFAULT_WORKSPACE_ID)
        )
    ).scalars().all()

    # old_model_id -> new_model_id mapping (used to map system_*_model_id and templates.model_id)
    model_id_map: dict[uuid.UUID, uuid.UUID] = {}
    for old_provider in default_providers:
        new_provider = Provider(
            workspace_id=workspace.id,
            name=old_provider.name,
            api_key=old_provider.api_key,
            endpoint=old_provider.endpoint,
        )
        db.add(new_provider)
        await db.flush()

        old_models = (
            await db.execute(
                select(LLMModel).where(LLMModel.provider_id == old_provider.id)
            )
        ).scalars().all()
        for om in old_models:
            new_model = LLMModel(
                provider_id=new_provider.id,
                display_name=om.display_name,
                api_name=om.api_name,
                input_price_per_1m_usd=om.input_price_per_1m_usd,
                output_price_per_1m_usd=om.output_price_per_1m_usd,
            )
            db.add(new_model)
            await db.flush()
            model_id_map[om.id] = new_model.id

    # Mirror the default workspace's system model assignments
    default_ws = await db.get(Workspace, DEFAULT_WORKSPACE_ID)
    if default_ws:
        if default_ws.orchestrator_model_id:
            workspace.orchestrator_model_id = model_id_map.get(
                default_ws.orchestrator_model_id
            )
        if default_ws.chat_model_id:
            workspace.chat_model_id = model_id_map.get(default_ws.chat_model_id)
        if default_ws.memory_extractor_model_id:
            workspace.memory_extractor_model_id = model_id_map.get(
                default_ws.memory_extractor_model_id
            )
        if default_ws.quality_judge_model_id:
            workspace.quality_judge_model_id = model_id_map.get(
                default_ws.quality_judge_model_id
            )

    # Clone the default workspace's quality rubrics (E-02); map old→new id so a
    # template's rubric_id points at the cloned rubric.
    rubric_id_map: dict[uuid.UUID, uuid.UUID] = {}
    default_rubrics = (
        await db.execute(
            select(Rubric).where(Rubric.workspace_id == DEFAULT_WORKSPACE_ID)
        )
    ).scalars().all()
    for r in default_rubrics:
        new_rubric = Rubric(
            workspace_id=workspace.id,
            name=r.name,
            description=r.description,
            applies_to=r.applies_to,
            is_default=r.is_default,
            dimensions=[dict(d) for d in (r.dimensions or [])],
        )
        db.add(new_rubric)
        await db.flush()
        rubric_id_map[r.id] = new_rubric.id

    # Copy the default workspace's Tool & MCP Registry (SPA-41) first, so the seeded
    # templates can reference this workspace's own entries (never cross-tenant ones).
    from app.registry.service import copy_registry_to_workspace

    registry_id_map = await copy_registry_to_workspace(
        db, DEFAULT_WORKSPACE_ID, workspace.id
    )

    # Seed the new workspace with copies of the default workspace's templates.
    defaults = (
        await db.execute(
            select(Template).where(Template.workspace_id == DEFAULT_WORKSPACE_ID)
        )
    ).scalars().all()
    for t in defaults:
        db.add(Template(
            name=t.name,
            description=t.description,
            soul_md=t.soul_md,
            model_id=model_id_map.get(t.model_id) if t.model_id else None,
            rubric_id=rubric_id_map.get(t.rubric_id) if t.rubric_id else None,
            tool_ids=[registry_id_map[i] for i in (t.tool_ids or []) if i in registry_id_map],
            max_ram=t.max_ram,
            max_cpu=t.max_cpu,
            timeout_minutes=t.timeout_minutes,
            tags=list(t.tags or []),
            workspace_id=workspace.id,
        ))

    await db.commit()

    settings = get_settings()
    return TokenOut(
        access_token=create_access_token(user.id, workspace.id),
        expires_in=settings.jwt_expires_minutes * 60,
        user=UserOut(id=user.id, email=user.email, display_name=user.display_name),
        default_workspace_id=workspace.id,
    )


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)) -> TokenOut:
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if not user or not user.is_active or not verify_password(body.password, user.password_hash or ""):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    membership = await db.execute(
        select(WorkspaceMember.workspace_id)
        .where(WorkspaceMember.user_id == user.id)
        .order_by(WorkspaceMember.created_at)
        .limit(1)
    )
    default_ws = membership.scalar_one_or_none()
    if not default_ws:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "user has no workspace")

    settings = get_settings()
    return TokenOut(
        access_token=create_access_token(user.id, default_ws),
        expires_in=settings.jwt_expires_minutes * 60,
        user=UserOut(id=user.id, email=user.email, display_name=user.display_name),
        default_workspace_id=default_ws,
    )


@router.get("/me")
async def me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id)
    )
    workspaces = [
        {"id": ws.id, "name": ws.name, "slug": ws.slug, "role": role}
        for ws, role in result.all()
    ]
    return {
        "user": {"id": user.id, "email": user.email, "display_name": user.display_name},
        "workspaces": workspaces,
    }
