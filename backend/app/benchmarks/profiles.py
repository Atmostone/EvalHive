"""Validate workspace references without exposing credential values."""

from __future__ import annotations

import re
import uuid

from evalhive_benchmark.protocol import validate_config
from sqlalchemy import select

from app.benchmarks.catalog import get_manifest
from app.models.benchmark import BenchmarkSecret
from app.models.provider import LLMModel, Provider


class ProfileError(ValueError):
    pass


def reject_inline_credentials(config: dict) -> None:
    names = {"apikey", "secret", "password", "token", "authorization", "accesstoken", "refreshtoken"}

    def walk(value):
        if isinstance(value, dict):
            for name, child in value.items():
                if re.sub(r"[_-]", "", name).lower() in names:
                    raise ProfileError("Inline credentials are not allowed; use secret references")
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(config)


async def validate_profile(db, workspace_id, *, connector_id, connector_version, config, secret_refs, model_bindings):
    manifest = get_manifest(connector_id, connector_version)
    validate_config(manifest["config_schema"], config)
    reject_inline_credentials(config)
    secret_schema = manifest.get("secret_schema", {"type": "object", "additionalProperties": False})
    properties = secret_schema.get("properties", {})
    if secret_schema.get("additionalProperties") is False and any(name not in properties for name in secret_refs):
        raise ProfileError("Unknown benchmark credential name")
    canonical_refs, canonical_models = {}, {}
    for name, reference in secret_refs.items():
        try:
            identity = uuid.UUID(reference)
        except (ValueError, TypeError, AttributeError):
            raise ProfileError("Invalid benchmark secret reference") from None
        if not await db.scalar(select(BenchmarkSecret.id).where(
            BenchmarkSecret.id == identity, BenchmarkSecret.workspace_id == workspace_id
        )):
            raise ProfileError("Benchmark secret reference is unavailable in this workspace")
        canonical_refs[name] = str(identity)
    for role, reference in model_bindings.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}", role):
            raise ProfileError("Invalid benchmark model role")
        try:
            identity = uuid.UUID(reference)
        except (ValueError, TypeError, AttributeError):
            raise ProfileError("Invalid benchmark model reference") from None
        if not await db.scalar(select(LLMModel.id).join(Provider).where(
            LLMModel.id == identity, Provider.workspace_id == workspace_id
        )):
            raise ProfileError("Benchmark model is unavailable in this workspace")
        canonical_models[role] = str(identity)
    missing = ["secret." + name for name in secret_schema.get("required", []) if name not in canonical_refs]
    missing += [capability for capability in manifest["requirements"].get("capabilities", [])
                if capability.startswith("model.") and capability[6:] not in canonical_models]
    return {"manifest": manifest, "secret_refs": canonical_refs, "model_bindings": canonical_models,
            "configured": not missing, "missing": sorted(missing)}
