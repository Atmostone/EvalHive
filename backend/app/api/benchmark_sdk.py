"""Installed benchmark metadata, workspace profiles and write-only credentials."""

from __future__ import annotations

import uuid

from evalhive_benchmark import ProtocolError
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import load_only

from app.auth.dependencies import get_current_workspace, require_role
from app.benchmarks.catalog import CatalogError, get_manifest, load_catalog, public_case
from app.benchmarks.datasets import DatasetConflict, register_dataset
from app.benchmarks.preflight import static_preflight
from app.benchmarks.profiles import ProfileError, validate_profile
from app.database import get_db
from app.models.benchmark import (
    BenchmarkDatasetCase,
    BenchmarkDatasetRevision,
    BenchmarkProfile,
    BenchmarkSecret,
)
from app.models.harness_profile import HarnessProfile
from app.models.workspace import Workspace


class SafeValidationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request):
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(422, "Invalid benchmark request") from None

        return safe


router = APIRouter(
    prefix="/api/benchmarks", tags=["benchmark-sdk"], route_class=SafeValidationRoute
)
profiles_router = APIRouter(
    prefix="/api/benchmark-profiles", tags=["benchmark-sdk"], route_class=SafeValidationRoute
)
secrets_router = APIRouter(
    prefix="/api/benchmark-secrets", tags=["benchmark-sdk"], route_class=SafeValidationRoute
)
datasets_router = APIRouter(
    prefix="/api/benchmark-datasets", tags=["benchmark-sdk"], route_class=SafeValidationRoute
)
ADMIN = Depends(require_role("owner", "admin"))


class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    connector_id: str = Field(min_length=1, max_length=200)
    connector_version: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict = Field(default_factory=dict, max_length=128)
    secret_refs: dict[str, str] = Field(default_factory=dict, max_length=128)
    model_bindings: dict[str, str] = Field(default_factory=dict, max_length=32)


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"\S")
    connector_version: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict | None = Field(default=None, max_length=128)
    secret_refs: dict[str, str] | None = Field(default=None, max_length=128)
    model_bindings: dict[str, str] | None = Field(default=None, max_length=32)


class SecretInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200, pattern=r"\S")
    value: SecretStr = Field(min_length=1, max_length=65536)


class DatasetRegister(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: uuid.UUID
    dataset_id: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=4096)


class PreflightInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: uuid.UUID
    dataset_revision_id: uuid.UUID
    case_id: uuid.UUID
    harness_profile_id: uuid.UUID


async def _lock(db, workspace):
    await db.execute(select(Workspace.id).where(Workspace.id == workspace.id).with_for_update())


async def _row(db, workspace, model, identity):
    statement = select(model).where(model.id == identity, model.workspace_id == workspace.id)
    if model is BenchmarkSecret:
        statement = statement.options(
            load_only(BenchmarkSecret.id, BenchmarkSecret.name, BenchmarkSecret.workspace_id)
        )
    value = await db.scalar(statement)
    if value is None:
        raise HTTPException(404, "Benchmark record not found")
    return value


async def _commit(db):
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409, "Benchmark configuration conflicts with an existing record"
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
                    "model_bindings",
                )
            },
        )
    except (CatalogError, ProfileError, ProtocolError) as error:
        raise HTTPException(422, str(error)) from None


async def profile_dict(db, workspace, profile):
    value = {
        key: getattr(profile, key)
        for key in (
            "name",
            "connector_id",
            "connector_version",
            "config",
            "secret_refs",
            "model_bindings",
        )
    }
    result = {
        "id": str(profile.id),
        **value,
        "created_at": profile.created_at.isoformat(),
        "updated_at": profile.updated_at.isoformat(),
        "installed": False,
        "configured": False,
        "compatible": None,
        "live_verified": False,
    }
    try:
        get_manifest(profile.connector_id, profile.connector_version)
        result["installed"] = True
        checked = await validate_profile(
            db, workspace.id, **{key: value[key] for key in value if key != "name"}
        )
        result.update(installed=True, configured=checked["configured"], missing=checked["missing"])
    except (CatalogError, ProfileError, ProtocolError):
        result["missing"] = ["profile_validation"]
    return result


def dataset_dict(dataset):
    return {
        "id": str(dataset.id),
        "connector_id": dataset.connector_id,
        "connector_version": dataset.connector_version,
        "dataset_id": dataset.dataset_id,
        "revision": dataset.revision,
        "source_digest": dataset.source_digest,
        "receipt_digest": dataset.receipt_digest,
        "worker_image": dataset.worker_image,
        "case_count": dataset.case_count,
        "created_at": dataset.created_at.isoformat(),
        "live_verified": False,
    }


@router.get("")
async def installed(workspace=Depends(get_current_workspace)):
    try:
        return [
            {
                "manifest": manifest,
                "installed": True,
                "configured": None,
                "compatible": None,
                "live_verified": False,
            }
            for manifest in load_catalog().values()
        ]
    except CatalogError:
        raise HTTPException(503, "Benchmark catalog is unavailable") from None


@profiles_router.get("")
async def list_profiles(workspace=Depends(get_current_workspace), db=Depends(get_db)):
    rows = await db.scalars(
        select(BenchmarkProfile)
        .where(BenchmarkProfile.workspace_id == workspace.id)
        .order_by(BenchmarkProfile.name)
    )
    return [await profile_dict(db, workspace, row) for row in rows]


@router.post("/preflight")
async def preflight(
    body: PreflightInput, workspace=Depends(get_current_workspace), db=Depends(get_db)
):
    profile = await _row(db, workspace, BenchmarkProfile, body.profile_id)
    dataset = await _row(db, workspace, BenchmarkDatasetRevision, body.dataset_revision_id)
    harness = await _row(db, workspace, HarnessProfile, body.harness_profile_id)
    case = await db.scalar(
        select(BenchmarkDatasetCase).where(
            BenchmarkDatasetCase.id == body.case_id,
            BenchmarkDatasetCase.dataset_revision_id == dataset.id,
        )
    )
    if case is None:
        raise HTTPException(404, "Benchmark case not found")
    if (dataset.connector_id, dataset.connector_version) != (
        profile.connector_id,
        profile.connector_version,
    ):
        raise HTTPException(422, "Dataset revision belongs to a different benchmark")
    checked = await _validate(
        db,
        workspace,
        {
            key: getattr(profile, key)
            for key in (
                "connector_id",
                "connector_version",
                "config",
                "secret_refs",
                "model_bindings",
            )
        },
    )
    return await static_preflight(db, workspace, profile, checked, dataset, case, harness)


@profiles_router.get("/{profile_id}")
async def read_profile(
    profile_id: uuid.UUID, workspace=Depends(get_current_workspace), db=Depends(get_db)
):
    return await profile_dict(
        db, workspace, await _row(db, workspace, BenchmarkProfile, profile_id)
    )


@profiles_router.post("", status_code=201)
async def create_profile(
    body: ProfileCreate, workspace=Depends(get_current_workspace), db=Depends(get_db), _role=ADMIN
):
    await _lock(db, workspace)
    values = body.model_dump()
    try:
        values["connector_version"] = get_manifest(body.connector_id, body.connector_version)[
            "version"
        ]
    except CatalogError as error:
        raise HTTPException(422, str(error)) from None
    checked = await _validate(db, workspace, values)
    values.update(secret_refs=checked["secret_refs"], model_bindings=checked["model_bindings"])
    profile = BenchmarkProfile(workspace_id=workspace.id, **values)
    db.add(profile)
    await _commit(db)
    await db.refresh(profile)
    return await profile_dict(db, workspace, profile)


@profiles_router.patch("/{profile_id}")
async def update_profile(
    profile_id: uuid.UUID,
    body: ProfileUpdate,
    workspace=Depends(get_current_workspace),
    db=Depends(get_db),
    _role=ADMIN,
):
    await _lock(db, workspace)
    profile = await _row(db, workspace, BenchmarkProfile, profile_id)
    changes = body.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise HTTPException(422, "Clear benchmark references with an empty object")
    values = {
        key: getattr(profile, key)
        for key in ("connector_id", "connector_version", "config", "secret_refs", "model_bindings")
    }
    values.update({key: value for key, value in changes.items() if key in values})
    checked = await _validate(db, workspace, values)
    changes.update(secret_refs=checked["secret_refs"], model_bindings=checked["model_bindings"])
    for key, value in changes.items():
        setattr(profile, key, value)
    await _commit(db)
    await db.refresh(profile)
    return await profile_dict(db, workspace, profile)


@profiles_router.post("/{profile_id}/validate")
async def validate_saved_profile(
    profile_id: uuid.UUID, workspace=Depends(get_current_workspace), db=Depends(get_db), _role=ADMIN
):
    profile = await _row(db, workspace, BenchmarkProfile, profile_id)
    result = await _validate(
        db,
        workspace,
        {
            key: getattr(profile, key)
            for key in (
                "connector_id",
                "connector_version",
                "config",
                "secret_refs",
                "model_bindings",
            )
        },
    )
    return {
        "valid": True,
        "configured": result["configured"],
        "missing": result["missing"],
        "live_verified": False,
    }


@profiles_router.delete("/{profile_id}", status_code=204)
async def delete_profile(
    profile_id: uuid.UUID, workspace=Depends(get_current_workspace), db=Depends(get_db), _role=ADMIN
):
    await _lock(db, workspace)
    await db.delete(await _row(db, workspace, BenchmarkProfile, profile_id))
    await _commit(db)


@secrets_router.get("")
async def list_secrets(workspace=Depends(get_current_workspace), db=Depends(get_db)):
    rows = await db.execute(
        select(BenchmarkSecret.id, BenchmarkSecret.name)
        .where(BenchmarkSecret.workspace_id == workspace.id)
        .order_by(BenchmarkSecret.name)
    )
    return [{"id": str(row.id), "name": row.name, "configured": True} for row in rows]


@secrets_router.post("", status_code=201)
async def create_secret(
    body: SecretInput, workspace=Depends(get_current_workspace), db=Depends(get_db), _role=ADMIN
):
    await _lock(db, workspace)
    secret = BenchmarkSecret(
        workspace_id=workspace.id, name=body.name, value=body.value.get_secret_value()
    )
    db.add(secret)
    await _commit(db)
    return {"id": str(secret.id), "name": secret.name, "configured": True}


@secrets_router.put("/{secret_id}")
async def replace_secret(
    secret_id: uuid.UUID,
    body: SecretInput,
    workspace=Depends(get_current_workspace),
    db=Depends(get_db),
    _role=ADMIN,
):
    await _lock(db, workspace)
    secret = await _row(db, workspace, BenchmarkSecret, secret_id)
    secret.name, secret.value = body.name, body.value.get_secret_value()
    await _commit(db)
    return {"id": str(secret.id), "name": secret.name, "configured": True}


@secrets_router.delete("/{secret_id}", status_code=204)
async def delete_secret(
    secret_id: uuid.UUID, workspace=Depends(get_current_workspace), db=Depends(get_db), _role=ADMIN
):
    await _lock(db, workspace)
    secret = await _row(db, workspace, BenchmarkSecret, secret_id)
    profiles = await db.scalars(
        select(BenchmarkProfile).where(BenchmarkProfile.workspace_id == workspace.id)
    )
    if any(str(secret_id) in profile.secret_refs.values() for profile in profiles):
        raise HTTPException(409, "Secret is referenced by a benchmark profile")
    await db.delete(secret)
    await _commit(db)


@datasets_router.get("")
async def list_datasets(workspace=Depends(get_current_workspace), db=Depends(get_db)):
    rows = await db.scalars(
        select(BenchmarkDatasetRevision)
        .where(BenchmarkDatasetRevision.workspace_id == workspace.id)
        .order_by(BenchmarkDatasetRevision.created_at)
    )
    return [dataset_dict(row) for row in rows]


@datasets_router.post("", status_code=201)
async def register_installed_dataset(
    body: DatasetRegister, workspace=Depends(get_current_workspace), db=Depends(get_db), _role=ADMIN
):
    await _lock(db, workspace)
    profile = await _row(db, workspace, BenchmarkProfile, body.profile_id)
    try:
        dataset = await register_dataset(db, workspace.id, profile, body.dataset_id, body.revision)
    except DatasetConflict as error:
        raise HTTPException(409, str(error)) from None
    except (CatalogError, ProtocolError) as error:
        raise HTTPException(422, str(error)) from None
    await _commit(db)
    return dataset_dict(dataset)


@datasets_router.get("/{dataset_id}")
async def read_dataset(
    dataset_id: uuid.UUID, workspace=Depends(get_current_workspace), db=Depends(get_db)
):
    return dataset_dict(await _row(db, workspace, BenchmarkDatasetRevision, dataset_id))


@datasets_router.get("/{dataset_id}/cases")
async def list_cases(
    dataset_id: uuid.UUID,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    workspace=Depends(get_current_workspace),
    db=Depends(get_db),
):
    dataset = await _row(db, workspace, BenchmarkDatasetRevision, dataset_id)
    rows = await db.scalars(
        select(BenchmarkDatasetCase)
        .where(BenchmarkDatasetCase.dataset_revision_id == dataset.id)
        .order_by(BenchmarkDatasetCase.position)
        .offset(offset)
        .limit(limit)
    )
    return {
        "total": dataset.case_count,
        "offset": offset,
        "cases": [{"id": str(row.id), **public_case(row.descriptor)} for row in rows],
    }
