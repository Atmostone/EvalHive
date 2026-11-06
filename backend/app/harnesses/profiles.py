"""Workspace-scoped profile validation and private credential resolution."""

from __future__ import annotations

import uuid

from evalhive_harness import validate_run
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.harnesses.catalog import get_manifest
from app.models.harness_profile import HarnessProfile, HarnessSecret
from app.models.provider import LLMModel, Provider
from app.models.workspace import Workspace


class ProfileError(ValueError):
    pass


def _reject_inline_secrets(config: dict, schema: dict) -> None:
    names = {
        "apikey",
        "password",
        "secret",
        "token",
        "authorization",
        "accesstoken",
        "refreshtoken",
    }

    def schema_fields(value):
        if isinstance(value, dict):
            for key, prop in value.get("properties", {}).items():
                if isinstance(prop, dict) and (
                    prop.get("writeOnly")
                    or prop.get("format") == "password"
                    or prop.get("x-secret")
                ):
                    names.add(key.replace("_", "").replace("-", "").lower())
            for child in value.values():
                schema_fields(child)
        elif isinstance(value, list):
            for child in value:
                schema_fields(child)

    def check(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.replace("_", "").replace("-", "").lower() in names:
                    raise ProfileError("Inline credentials are not allowed; use secret references")
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    schema_fields(schema)
    check(config)


async def validate_profile(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    connector_id: str,
    connector_version: str,
    config: dict,
    secret_refs: dict,
    model_id: uuid.UUID | None,
) -> dict:
    manifest = get_manifest(connector_id, connector_version)
    _reject_inline_secrets(config, manifest["config_schema"])
    validate_run(
        manifest,
        {
            "protocol_version": "1.0",
            "workspace_id": str(workspace_id),
            "task_id": "preflight",
            "attempt_id": "preflight",
            "task": {"prompt": "Validate profile"},
            "cwd": "/workspace",
            "config": config,
        },
    )
    if model_id is not None:
        model = await db.scalar(
            select(LLMModel.id)
            .join(Provider)
            .where(LLMModel.id == model_id, Provider.workspace_id == workspace_id)
        )
        if model is None:
            raise ProfileError("Model is unavailable in this workspace")
    canonical_refs = {}
    for name, reference in secret_refs.items():
        try:
            secret_id = uuid.UUID(reference)
        except (ValueError, TypeError, AttributeError):
            raise ProfileError("Invalid secret reference") from None
        exists = await db.scalar(
            select(HarnessSecret.id).where(
                HarnessSecret.id == secret_id, HarnessSecret.workspace_id == workspace_id
            )
        )
        if exists is None:
            raise ProfileError("Secret reference is unavailable in this workspace")
        canonical_refs[name] = str(secret_id)
    return canonical_refs


async def seed_workspace_profile(db: AsyncSession, workspace_id: uuid.UUID) -> HarnessProfile:
    manifest = get_manifest("pi")
    profile = HarnessProfile(
        workspace_id=workspace_id,
        name="Pi",
        connector_id="pi",
        connector_version=manifest["version"],
        config={},
        secret_refs={},
        is_default=True,
    )
    db.add(profile)
    await db.flush()
    return profile


async def seed_missing_profiles(db: AsyncSession) -> None:
    # Startup can run concurrently in several API replicas. Row locks make the
    # check/insert atomic, with the partial unique index as a second guard.
    workspaces = await db.scalars(select(Workspace).order_by(Workspace.id).with_for_update())
    for workspace in workspaces:
        existing = await db.scalar(
            select(HarnessProfile.id).where(HarnessProfile.workspace_id == workspace.id).limit(1)
        )
        if existing is None:
            await seed_workspace_profile(db, workspace.id)
    await db.commit()


async def resolve_secret_refs(
    db: AsyncSession, workspace_id: uuid.UUID, references: dict
) -> dict[str, str]:
    """Runner-only. Never expose the returned mapping through an API or snapshot."""
    resolved = {}
    for name, reference in references.items():
        try:
            secret_id = uuid.UUID(reference)
        except (ValueError, TypeError, AttributeError):
            raise ProfileError("Invalid secret reference") from None
        secret = await db.scalar(
            select(HarnessSecret).where(
                HarnessSecret.id == secret_id, HarnessSecret.workspace_id == workspace_id
            )
        )
        if secret is None:
            raise ProfileError("Secret reference is unavailable in this workspace")
        resolved[name] = secret.value
    return resolved
