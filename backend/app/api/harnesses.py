"""Installed harness declarations, workspace profiles and write-only credentials."""

from __future__ import annotations

import uuid

from evalhive_harness import ProtocolError
from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_workspace, require_role
from app.database import get_db
from app.harnesses.catalog import CatalogError, get_manifest, load_catalog
from app.harnesses.profiles import ProfileError, validate_profile
from app.models.harness_profile import HarnessProfile, HarnessSecret
from app.models.workspace import Workspace


class SafeValidationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # FastAPI's default includes rejected input, including credential values.
                raise HTTPException(422, "Invalid harness request") from None

        return safe_handler


router = APIRouter(prefix="/api/harnesses", tags=["harnesses"], route_class=SafeValidationRoute)
profiles_router = APIRouter(
    prefix="/api/harness-profiles", tags=["harnesses"], route_class=SafeValidationRoute
)
secrets_router = APIRouter(
    prefix="/api/harness-secrets", tags=["harnesses"], route_class=SafeValidationRoute
)
ADMIN = Depends(require_role("owner", "admin"))


class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    connector_id: str = Field(min_length=1, max_length=200)
    connector_version: str | None = Field(default=None, min_length=1, max_length=100)
    config: dict = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict, max_length=100)
    model_id: uuid.UUID | None = None
    is_default: bool = False


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"\S")
    connector_version: str | None = Field(default=None, min_length=1, max_length=100)
    config: dict | None = None
    secret_refs: dict[str, str] | None = Field(default=None, max_length=100)
    model_id: uuid.UUID | None = None
    is_default: bool | None = None


class SecretInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    value: SecretStr = Field(min_length=1, max_length=65536)


def profile_dict(profile: HarnessProfile) -> dict:
    return {
        "id": str(profile.id),
        "name": profile.name,
        "connector_id": profile.connector_id,
        "connector_version": profile.connector_version,
        "config": profile.config,
        "secret_refs": profile.secret_refs,
        "model_id": str(profile.model_id) if profile.model_id else None,
        "is_default": profile.is_default,
        "created_at": profile.created_at.isoformat(),
        "updated_at": profile.updated_at.isoformat(),
    }


def secret_dict(secret: HarnessSecret) -> dict:
    return {"id": str(secret.id), "name": secret.name, "configured": True}


async def _workspace_lock(db, workspace):
    # Serialize default selection and reference/deletion races across API replicas.
    await db.execute(select(Workspace.id).where(Workspace.id == workspace.id).with_for_update())


async def _profile(db, workspace, profile_id):
    profile = await db.scalar(
        select(HarnessProfile).where(
            HarnessProfile.id == profile_id, HarnessProfile.workspace_id == workspace.id
        )
    )
    if profile is None:
        raise HTTPException(404, "Harness profile not found")
    return profile


async def _secret(db, workspace, secret_id):
    secret = await db.scalar(
        select(HarnessSecret).where(
            HarnessSecret.id == secret_id, HarnessSecret.workspace_id == workspace.id
        )
    )
    if secret is None:
        raise HTTPException(404, "Harness secret not found")
    return secret


async def _commit(db):
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409, "Harness configuration conflicts with an existing record"
        ) from None


async def _validate(db, workspace, values):
    try:
        return await validate_profile(
            db,
            workspace.id,
            **{
                key: values[key]
                for key in (
                    "connector_id",
                    "connector_version",
                    "config",
                    "secret_refs",
                    "model_id",
                )
            },
        )
    except (CatalogError, ProfileError, ProtocolError) as exc:
        raise HTTPException(422, str(exc)) from None


@router.get("")
async def list_harnesses(workspace: Workspace = Depends(get_current_workspace)):
    try:
        return list(load_catalog().values())
    except CatalogError:
        raise HTTPException(503, "Harness catalog is unavailable") from None


@profiles_router.get("")
async def list_profiles(
    workspace: Workspace = Depends(get_current_workspace), db: AsyncSession = Depends(get_db)
):
    rows = await db.scalars(
        select(HarnessProfile)
        .where(HarnessProfile.workspace_id == workspace.id)
        .order_by(HarnessProfile.name)
    )
    return [profile_dict(row) for row in rows]


@profiles_router.get("/{profile_id}")
async def read_profile(
    profile_id: uuid.UUID,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    return profile_dict(await _profile(db, workspace, profile_id))


@profiles_router.post("", status_code=201)
async def create_profile(
    body: ProfileCreate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=ADMIN,
):
    await _workspace_lock(db, workspace)
    values = body.model_dump()
    try:
        values["connector_version"] = get_manifest(body.connector_id, body.connector_version)[
            "version"
        ]
    except CatalogError as exc:
        raise HTTPException(422, str(exc)) from None
    values["secret_refs"] = await _validate(db, workspace, values)
    if body.is_default:
        await db.execute(
            update(HarnessProfile)
            .where(HarnessProfile.workspace_id == workspace.id, HarnessProfile.is_default.is_(True))
            .values(is_default=False)
        )
    profile = HarnessProfile(workspace_id=workspace.id, **values)
    db.add(profile)
    await _commit(db)
    await db.refresh(profile)
    return profile_dict(profile)


@profiles_router.patch("/{profile_id}")
async def update_profile(
    profile_id: uuid.UUID,
    body: ProfileUpdate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=ADMIN,
):
    await _workspace_lock(db, workspace)
    profile = await _profile(db, workspace, profile_id)
    changes = body.model_dump(exclude_unset=True)
    if any(value is None for key, value in changes.items() if key != "model_id"):
        raise HTTPException(422, "Only model_id can be cleared with null")
    if changes.get("is_default") is False and profile.is_default:
        raise HTTPException(409, "Select another default profile before clearing this one")
    values = {
        key: getattr(profile, key)
        for key in ("connector_id", "connector_version", "config", "secret_refs", "model_id")
    }
    values.update({key: value for key, value in changes.items() if key in values})
    changes["secret_refs"] = await _validate(db, workspace, values)
    if changes.get("is_default"):
        await db.execute(
            update(HarnessProfile)
            .where(HarnessProfile.workspace_id == workspace.id, HarnessProfile.is_default.is_(True))
            .values(is_default=False)
        )
    for key, value in changes.items():
        setattr(profile, key, value)
    await _commit(db)
    await db.refresh(profile)
    return profile_dict(profile)


@profiles_router.post("/{profile_id}/validate")
async def validate_saved_profile(
    profile_id: uuid.UUID,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=ADMIN,
):
    profile = await _profile(db, workspace, profile_id)
    await _validate(
        db,
        workspace,
        {
            key: getattr(profile, key)
            for key in ("connector_id", "connector_version", "config", "secret_refs", "model_id")
        },
    )
    return {"valid": True}


@profiles_router.delete("/{profile_id}", status_code=204)
async def delete_profile(
    profile_id: uuid.UUID,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=ADMIN,
):
    await _workspace_lock(db, workspace)
    profile = await _profile(db, workspace, profile_id)
    if profile.is_default:
        raise HTTPException(409, "Select another default profile before deleting this one")
    await db.delete(profile)
    await _commit(db)


@secrets_router.get("")
async def list_secrets(
    workspace: Workspace = Depends(get_current_workspace), db: AsyncSession = Depends(get_db)
):
    rows = await db.scalars(
        select(HarnessSecret)
        .where(HarnessSecret.workspace_id == workspace.id)
        .order_by(HarnessSecret.name)
    )
    return [secret_dict(row) for row in rows]


@secrets_router.post("", status_code=201)
async def create_secret(
    body: SecretInput,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=ADMIN,
):
    secret = HarnessSecret(
        workspace_id=workspace.id, name=body.name, value=body.value.get_secret_value()
    )
    db.add(secret)
    await _commit(db)
    await db.refresh(secret)
    return secret_dict(secret)


@secrets_router.put("/{secret_id}")
async def replace_secret(
    secret_id: uuid.UUID,
    body: SecretInput,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=ADMIN,
):
    await _workspace_lock(db, workspace)
    secret = await _secret(db, workspace, secret_id)
    secret.name, secret.value = body.name, body.value.get_secret_value()
    await _commit(db)
    await db.refresh(secret)
    return secret_dict(secret)


@secrets_router.delete("/{secret_id}", status_code=204)
async def delete_secret(
    secret_id: uuid.UUID,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=ADMIN,
):
    await _workspace_lock(db, workspace)
    secret = await _secret(db, workspace, secret_id)
    profiles = await db.scalars(
        select(HarnessProfile).where(HarnessProfile.workspace_id == workspace.id)
    )
    if any(str(secret_id) in profile.secret_refs.values() for profile in profiles):
        raise HTTPException(409, "Secret is referenced by a harness profile")
    await db.delete(secret)
    await _commit(db)
