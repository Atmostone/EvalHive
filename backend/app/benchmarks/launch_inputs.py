"""Freeze native benchmark and Harness SDK inputs without starting an agent."""

from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace

from evalhive_benchmark import validate_spec
from evalhive_harness import validate_run
from sqlalchemy import select

from app.benchmarks.catalog import merged_requirements
from app.benchmarks.installations import resolve_package
from app.benchmarks.preflight import static_preflight
from app.benchmarks.profiles import validate_profile
from app.benchmarks.trials import failure
from app.harnesses.catalog import get_manifest as harness_manifest
from app.harnesses.profiles import resolve_secret_refs
from app.harnesses.profiles import validate_profile as validate_harness_profile
from app.models.benchmark import (
    BenchmarkDatasetCase,
    BenchmarkDatasetRevision,
    BenchmarkProfile,
    BenchmarkSecret,
)
from app.models.harness_profile import HarnessProfile
from app.models.provider import LLMModel, Provider
from app.runtime.model_gateway import ModelRoute


@dataclass(frozen=True)
class FrozenInputs:
    snapshot: dict
    private: dict
    title: str
    description: str
    harness_profile_id: uuid.UUID


async def scoped(db, model, workspace_id, identity):
    row = await db.scalar(
        select(model).where(
            model.id == uuid.UUID(str(identity)), model.workspace_id == workspace_id
        )
    )
    if row is None:
        raise failure(
            "SELECTION_UNAVAILABLE", "Benchmark selection is unavailable in this workspace"
        )
    return row


async def connection(db, workspace_id, reference):
    if reference is None:
        raise failure("MODEL_UNAVAILABLE", "Choose a model connection before execution")
    pair = (
        await db.execute(
            select(LLMModel, Provider)
            .join(Provider)
            .where(
                LLMModel.id == uuid.UUID(str(reference)),
                Provider.workspace_id == workspace_id,
            )
        )
    ).first()
    if pair is None:
        raise failure("MODEL_UNAVAILABLE", "Model connection is unavailable in this workspace")
    model, provider = pair
    try:
        ModelRoute(model.api_name, provider.endpoint, provider.api_key, "validation-only")
    except ValueError:
        raise failure(
            "MODEL_UNAVAILABLE", "Model connection is incomplete or its endpoint is invalid"
        ) from None
    return model, provider


def required_roles(requirements, prefix):
    return {
        name[len(prefix) :]
        for name in requirements.get("capabilities", [])
        if name.startswith(prefix)
    }


async def resolve_inputs(
    db,
    workspace,
    selection,
    *,
    trial_id,
    task_id,
    limits,
    agent_max_turns=100,
    benchmark_config=None,
    harness_config=None,
    model_id=None,
):
    profile = await scoped(db, BenchmarkProfile, workspace.id, selection["profile_id"])
    dataset = await scoped(
        db, BenchmarkDatasetRevision, workspace.id, selection["dataset_revision_id"]
    )
    harness = await scoped(db, HarnessProfile, workspace.id, selection["harness_profile_id"])
    case = await db.scalar(
        select(BenchmarkDatasetCase).where(
            BenchmarkDatasetCase.id == uuid.UUID(str(selection["case_id"])),
            BenchmarkDatasetCase.dataset_revision_id == dataset.id,
        )
    )
    if case is None:
        raise failure("SELECTION_UNAVAILABLE", "Benchmark case is unavailable in this workspace")
    if (profile.connector_id, profile.connector_version) != (
        dataset.connector_id,
        dataset.connector_version,
    ):
        raise failure(
            "SELECTION_MISMATCH", "Dataset revision belongs to another benchmark connector"
        )
    models = dict(profile.model_bindings)
    if model_id is not None:
        models["agent"] = str(model_id)
    configured = SimpleNamespace(
        connector_id=profile.connector_id,
        connector_version=profile.connector_version,
        config={**deepcopy(profile.config), **(benchmark_config or {})},
        secret_refs=deepcopy(profile.secret_refs),
        model_bindings=models,
    )
    checked = await validate_profile(db, workspace.id, **vars(configured))
    hconfig = {**deepcopy(harness.config), **(harness_config or {})}
    await validate_harness_profile(
        db,
        workspace.id,
        connector_id=harness.connector_id,
        connector_version=harness.connector_version,
        config=hconfig,
        secret_refs=harness.secret_refs,
        model_id=harness.model_id,
    )
    preflight = await static_preflight(db, workspace, configured, checked, dataset, case, harness)
    if not preflight["configured"]:
        raise failure(
            "CONFIGURATION_INCOMPLETE",
            "Configure all required model and credential references before execution",
        )
    if not preflight["compatible"]:
        raise failure(
            "SELECTION_INCOMPATIBLE",
            "Selected benchmark, case, harness and execution host are incompatible; check compatibility in Settings",
        )
    package = resolve_package(dataset)
    declaration = package.manifest
    requirements = merged_requirements(declaration["requirements"], case.descriptor["requirements"])
    roles = required_roles(requirements, "model.") - {"agent"}
    secret_roles = set(declaration.get("secret_schema", {}).get("required", [])) | required_roles(
        requirements, "credential."
    )
    native_models, auxiliary, credential_refs, native_secrets = {}, {}, {}, {}
    for role in sorted(roles):
        model, provider = await connection(db, workspace.id, models.get(role))
        reference = "model." + role
        credential_refs[reference] = "runtime-injected"
        native_models[role] = {
            "name": model.api_name,
            "endpoint": provider.endpoint,
            "credential_ref": reference,
        }
        auxiliary[role] = {
            "name": model.api_name,
            "endpoint": provider.endpoint,
            "api_key": provider.api_key,
            "credential_ref": reference,
            "phases": ["driving", "evaluating"],
        }
    for role in sorted(secret_roles):
        reference = checked["secret_refs"].get(role)
        if reference is None:
            raise failure(
                "CONFIGURATION_INCOMPLETE",
                "Configure the required benchmark credential before execution",
            )
        if role in credential_refs or role == "agent.credentials":
            raise failure(
                "CREDENTIAL_CONFLICT",
                "Benchmark credential role conflicts with a reserved runtime reference",
            )
        secret = await scoped(db, BenchmarkSecret, workspace.id, reference)
        native_secrets[role] = secret.value
        credential_refs[role] = "runtime-injected"
    hmanifest = harness_manifest(harness.connector_id, harness.connector_version)
    if hmanifest["capabilities"].get("model_gateway") not in {"native", "emulated"}:
        raise failure(
            "UNSUPPORTED_HARNESS", "The selected harness cannot use the benchmark model connection"
        )
    agent_model, agent_provider = await connection(
        db, workspace.id, models.get("agent") or harness.model_id
    )
    agent_credentials = await resolve_secret_refs(db, workspace.id, harness.secret_refs)
    if "model_api_key" in agent_credentials:
        raise failure(
            "CREDENTIAL_CONFLICT", "Harness credential role conflicts with the model connection"
        )
    agent_credentials["model_api_key"] = agent_provider.api_key
    hspec = {
        "protocol_version": "1.0",
        "workspace_id": str(workspace.id),
        "task_id": str(task_id),
        "attempt_id": str(uuid.uuid4()),
        "task": {"prompt": "Bound by native benchmark preparation"},
        "cwd": "/workspace",
        "config": hconfig,
        "limits": {"wall_time_seconds": limits["wall_time_seconds"], "max_turns": agent_max_turns},
        "model": {
            "name": agent_model.api_name,
            "endpoint": agent_provider.endpoint,
            "api": "openai-compatible",
            "credential_ref": "model_api_key",
        },
        "credential_refs": {name: "runtime-injected" for name in agent_credentials},
        "metadata": {
            "execution_mode": "direct",
            "profile_id": str(harness.id),
            "model_id": str(agent_model.id),
            "benchmark_trial_id": str(trial_id),
        },
        "interaction": {"version": "1.0", "mode": "single_turn"},
    }
    validate_run(hmanifest, hspec)
    spec = {
        "protocol_version": "1.0",
        "scope": {"workspace_id": str(workspace.id), "trial_id": str(trial_id), "generation": 1},
        "case": deepcopy(case.descriptor),
        "config": configured.config,
        "credential_refs": credential_refs,
        "models": native_models,
        "limits": deepcopy(limits),
    }
    validate_spec(declaration, spec)
    snapshot = {
        "format_version": "1.0",
        "selection": {
            key: str(selection[key])
            for key in ("profile_id", "dataset_revision_id", "case_id", "harness_profile_id")
        },
        "benchmark": {
            "id": declaration["id"],
            "version": declaration["version"],
            "worker_image": dataset.worker_image,
            "config": configured.config,
            "secret_refs": {name: checked["secret_refs"][name] for name in sorted(secret_roles)},
            "model_bindings": {role: models[role] for role in sorted(roles)},
        },
        "dataset": {
            "dataset_id": dataset.dataset_id,
            "revision": dataset.revision,
            "source_digest": dataset.source_digest,
            "receipt_digest": dataset.receipt_digest,
            "case": deepcopy(case.descriptor["ref"]),
            "case_source_digest": case.descriptor["source_digest"],
        },
        "harness": {
            "profile_id": str(harness.id),
            "connector_id": hmanifest["id"],
            "connector_version": hmanifest["version"],
            "model_id": str(agent_model.id),
            "model": agent_model.api_name,
            "config": hconfig,
            "secret_refs": deepcopy(harness.secret_refs),
        },
        "limits": deepcopy(limits),
        "agent_max_turns": agent_max_turns,
        "installation_metadata_digest": package.metadata_digest,
    }
    private = {
        "format_version": "1.0",
        "benchmark_manifest": declaration,
        "benchmark_spec": spec,
        "harness_manifest": hmanifest,
        "harness_spec": hspec,
        "harness_credentials": agent_credentials,
        "benchmark_private_inputs": native_secrets,
        "auxiliary_models": auxiliary,
        "installation_metadata_digest": package.metadata_digest,
    }
    return FrozenInputs(
        snapshot,
        private,
        case.descriptor["title"],
        case.descriptor.get("description", ""),
        harness.id,
    )


async def refresh_authorization(db, workspace_id, snapshot, payload, *, evaluation_roles=None):
    """Keep frozen model settings while honoring connection deletion and key rotation."""
    result = deepcopy(payload)
    bindings = snapshot["benchmark"]["model_bindings"]
    roles = set(bindings) if evaluation_roles is None else set(evaluation_roles)
    if not roles <= set(bindings):
        raise failure("MODEL_UNAVAILABLE", "Frozen evaluator model references are unavailable")
    for role in sorted(roles):
        model, provider = await connection(db, workspace_id, bindings[role])
        native = result["auxiliary_models"][role]
        if (model.api_name, provider.endpoint) != (native["name"], native["endpoint"]):
            raise failure(
                "MODEL_CHANGED",
                "The recorded model connection changed; create a run with new conditions",
            )
        native["api_key"] = provider.api_key
    if evaluation_roles is not None:
        return result
    model, provider = await connection(db, workspace_id, snapshot["harness"]["model_id"])
    agent = result["harness_spec"]["model"]
    if (model.api_name, provider.endpoint) != (agent["name"], agent["endpoint"]):
        raise failure(
            "MODEL_CHANGED",
            "The recorded agent connection changed; create a run with new conditions",
        )
    credentials = await resolve_secret_refs(db, workspace_id, snapshot["harness"]["secret_refs"])
    credentials["model_api_key"] = provider.api_key
    result["harness_credentials"] = credentials
    values = {}
    for role, reference in snapshot["benchmark"]["secret_refs"].items():
        secret = await scoped(db, BenchmarkSecret, workspace_id, reference)
        values[role] = secret.value
    result["benchmark_private_inputs"] = values
    return result
