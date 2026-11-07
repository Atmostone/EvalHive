"""Static compatibility before the resource broker's authoritative reservation."""

from __future__ import annotations

import platform
import shutil
import uuid

from sqlalchemy import select

from app.benchmarks.catalog import CatalogError, compatibility, merged_requirements
from app.benchmarks.host_contract import capabilities, group_policy
from app.config import get_settings
from app.harnesses.catalog import get_manifest as harness_manifest
from app.models.provider import LLMModel, Provider


def policy_capacity() -> dict:
    settings = get_settings()
    policy = group_policy(settings)
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(
        platform.machine(), platform.machine()
    )
    resources = {
        "memory_mb": policy.max_memory_mb,
        "cpu_cores": policy.max_cpu_cores,
    }
    try:
        resources["disk_mb"] = max(
            0, shutil.disk_usage(settings.data_dir).free // (1024 * 1024) - policy.disk_reserve_mb
        )
    except OSError:
        pass
    return {
        "capabilities": capabilities(settings),
        "resources": resources,
        "platform": {
            "os": platform.system().lower(),
            "architecture": architecture,
            "libc": platform.libc_ver()[0],
        },
    }


async def static_preflight(db, workspace, profile, checked, dataset, case, harness):
    declaration = checked["manifest"]
    original_requirements = {
        "benchmark": declaration["requirements"],
        "case": case.descriptor["requirements"],
    }
    try:
        native = harness_manifest(harness.connector_id, harness.connector_version)
    except ValueError:
        return {
            "installed": False,
            "configured": False,
            "compatible": False,
            "can_run": False,
            "live_verified": False,
            "stage": "static",
            "native_requirements": original_requirements,
            "reasons": [{"code": "harness_unavailable"}],
        }
    available = policy_capacity()
    available["capabilities"] += ["credential." + role for role in checked["secret_refs"]]
    available["capabilities"] += [
        "harness." + name
        for name, support in native["capabilities"].items()
        if support in {"native", "emulated"}
    ]
    bindings = {**profile.model_bindings}
    if "agent" not in bindings and harness.model_id:
        bindings["agent"] = str(harness.model_id)
    missing = list(checked["missing"])
    for role, reference in bindings.items():
        model = await db.scalar(
            select(LLMModel.id)
            .join(Provider)
            .where(
                LLMModel.id == uuid.UUID(reference),
                Provider.workspace_id == workspace.id,
                Provider.api_key != "",
                Provider.endpoint != "",
            )
        )
        if model:
            available["capabilities"].append("model." + role)
        else:
            missing.append("model." + role)
    if "agent" not in bindings:
        missing.append("model.agent")
    try:
        requirements = merged_requirements(
            declaration["requirements"], case.descriptor["requirements"]
        )
        result = compatibility(requirements, available)
    except CatalogError:
        result = {"compatible": False, "reasons": [{"code": "conflicting_native_requirements"}]}
    if dataset.manifest != declaration:
        result["compatible"] = False
        result["reasons"].append({"code": "installed_manifest_changed"})
    if not get_settings().benchmark_runtime_enabled:
        result["compatible"] = False
        result["reasons"].append({"code": "runtime_unavailable"})
    return {
        **result,
        "installed": True,
        "configured": not missing,
        "missing": sorted(set(missing)),
        "native_requirements": original_requirements,
        "live_verified": False,
        "stage": "static",
        "can_run": False,
        "runtime_admission": "pending",
        "dataset_revision_id": str(dataset.id),
        "case_id": str(case.id),
        "model_bindings": bindings,
        "harness_profile_id": str(harness.id),
    }
