"""Resolve task intent into the installed connector's neutral RunSpec."""

from __future__ import annotations

import re
import uuid
from copy import deepcopy
from dataclasses import dataclass
from urllib.parse import urlsplit

from evalhive_harness import validate_run
from sqlalchemy import select

from app.api._resolve_model import resolve_model_by_id
from app.api.settings import get_setting
from app.harnesses.catalog import get_manifest
from app.harnesses.executor import launch_attempt
from app.harnesses.profiles import ProfileError, resolve_secret_refs, validate_profile
from app.harnesses.resources import assemble_resources, workspace_context
from app.models.harness_profile import HarnessProfile
from app.models.provider import LLMModel
from app.models.task import Task
from app.models.template import Template
from app.registry.resolver import resolve_template_tools
from app.runtime.spec import NetworkSpec


@dataclass
class ResolvedLaunch:
    manifest: dict
    spec: dict
    credentials: dict
    profile: HarnessProfile
    network: NetworkSpec
    model: LLMModel | None


async def selected_template(db, task):
    if task.template_id is None:
        return None
    template = await db.get(Template, task.template_id)
    if template is None or template.workspace_id != task.workspace_id:
        raise ProfileError("Role template is unavailable in this workspace")
    return template


def execution_mode(task, template=None):
    return (getattr(task, "execution_mode", None) or (task.run_config or {}).get("execution_mode")
            or getattr(template, "execution_mode", None) or "orchestrated")


async def selected_profile(db, task, template=None):
    override = (task.run_config or {}).get("harness_profile_id")
    profile_id = task.harness_profile_id or override or getattr(template, "harness_profile_id", None)
    query = select(HarnessProfile).where(HarnessProfile.workspace_id == task.workspace_id)
    if profile_id:
        try:
            profile_id = uuid.UUID(str(profile_id))
        except ValueError:
            raise ProfileError("Invalid harness profile reference") from None
        query = query.where(HarnessProfile.id == profile_id)
    else:
        query = query.where(HarnessProfile.is_default.is_(True))
    profile = await db.scalar(query)
    if profile is None:
        raise ProfileError("Harness profile is unavailable in this workspace")
    await validate_profile(
        db, task.workspace_id, connector_id=profile.connector_id,
        connector_version=profile.connector_version, config=profile.config,
        secret_refs=profile.secret_refs, model_id=profile.model_id,
    )
    return profile


def memory_mb(value):
    match = re.fullmatch(r"([1-9][0-9]*)([kmgt]?)", str(value).lower())
    if not match:
        raise ProfileError("Invalid memory limit")
    size = int(match[1]) * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[match[2]]
    if size % (1024**2):
        raise ProfileError("Memory limit must be a whole number of MiB")
    return size // (1024**2)


async def resolve_launch(db, task, template=None):
    template = template if template is not None else await selected_template(db, task)
    if template is not None and template.workspace_id != task.workspace_id:
        raise ProfileError("Role template is unavailable in this workspace")
    profile = await selected_profile(db, task, template)
    manifest = get_manifest(profile.connector_id, profile.connector_version)
    overrides = task.run_config or {}
    frozen = overrides.get("harness_snapshot")
    changed = set(overrides.get("replay_overrides", []))
    if frozen:
        if (manifest["id"], manifest["version"]) != (frozen["connector_id"], frozen["connector_version"]):
            raise ProfileError("Recorded connector version is unavailable; create a run with new conditions")
    model_id = overrides.get("model_id") or (frozen or {}).get("model_id") or profile.model_id or getattr(template, "model_id", None)
    llm = None
    if not model_id and manifest.get("requirements", {}).get("model_api"):
        raise ProfileError("Choose a model connection in the harness profile or role before execution")
    if model_id:
        llm = await resolve_model_by_id(db, model_id)
        if llm.provider.workspace_id != task.workspace_id:
            raise ProfileError("Model connection is unavailable in this workspace")
        if not llm.provider.endpoint or not llm.provider.api_key:
            raise ProfileError("Model connection is incomplete")
        try:
            endpoint = urlsplit(llm.provider.endpoint)
        except ValueError:
            raise ProfileError("Model endpoint is invalid") from None
        if endpoint.scheme not in {"http", "https"} or not endpoint.netloc or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ProfileError("Model endpoint must be an HTTP URL without inline credentials")
    # Old executable overrides must not silently change the installed image or
    # discard a scientific treatment while moving to the SDK path.
    if overrides.get("agent_image") or overrides.get("tool_injection"):
        raise ProfileError("This run requires conversion of its legacy execution options")
    attempt_id = uuid.uuid4()
    mode = execution_mode(task, template)
    memory_mode = overrides.get("memory_mode") or await get_setting(db, "memory_mode", "flat")
    rules, memory = workspace_context(task.workspace_id, memory_mode)
    if memory_mode == "structured":
        from app.memory.store import build_memory_context

        memory = await build_memory_context(
            db, query_text=f"{task.title}\n{task.description or ''}", workspace_id=task.workspace_id
        )
    tools, servers = (None, []) if template is None else await resolve_template_tools(
        db, template, run_config=overrides
    )
    if pg_host := overrides.get("pg_host"):
        if overrides.get("environment_id") != "toolathlon" or not re.fullmatch(r"toolathlon_pg(?:_lane_[0-7])?", str(pg_host)):
            raise ProfileError("Database lane is not registered for this task environment")
        servers = deepcopy(servers)
        for server in servers:
            if not server.get("url"):
                server.setdefault("env", {}).update(PGHOST=str(pg_host), PG_HOST=str(pg_host))
    declarations, credentials = assemble_resources(
        tools=tools, mcp_servers=servers, memory_mode=memory_mode, memory_context=memory,
        rules=rules, attempt_id=attempt_id,
    )
    # Namespaces keep profile roles from shadowing server-owned/model credentials.
    profile_credentials = await resolve_secret_refs(db, task.workspace_id, profile.secret_refs)
    if set(profile_credentials) & (set(credentials) | {"model_api_key", "evalhive_knowledge_token"}):
        raise ProfileError("Harness credential role conflicts with a reserved runtime reference")
    credentials = {**profile_credentials, **credentials}
    if llm is not None:
        credentials["model_api_key"] = llm.provider.api_key
    instructions = overrides.get("soul_md", getattr(template, "soul_md", ""))
    prompt = f"{task.title}\n\n{task.description or ''}"
    for name in ("user_feedback", "orchestrator_feedback"):
        if feedback := getattr(task, name, None):
            prompt += f"\n\n{name.replace('_', ' ').upper()}:\n{feedback}"
    for dependency_id in task.depends_on or []:
        dependency = await db.get(Task, dependency_id)
        if dependency is None or dependency.workspace_id != task.workspace_id or dependency.status != "done":
            raise ProfileError("A task dependency is unavailable or incomplete")
        prompt += f"\n\nDependency: {dependency.title}\n{dependency.result_summary or ''}\nFiles: {dependency.result_files or []}"
    environment = {"id": overrides.get("environment_id", "workspace"), "resources": declarations,
                   "artifact_policy": {"root": ".", "recursive": True}}
    if environment["id"] != "workspace":
        environment["workspace_seed"] = "task"
    network = NetworkSpec()
    if network_mode := overrides.get("network_mode"):
        if not isinstance(network_mode, str) or not network_mode.startswith("container:"):
            raise ProfileError("Unsupported environment network mode")
        network = NetworkSpec(container=network_mode.removeprefix("container:"))
    model = ({"name": llm.model.api_name, "endpoint": llm.provider.endpoint, "api": "openai-compatible",
              "credential_ref": "model_api_key"} if llm is not None else None)
    sampling = {name: overrides[name] for name in ("temperature", "seed") if overrides.get(name) is not None}
    if sampling:
        if model is None:
            raise ProfileError("Sampling requires a model connection")
        model["sampling"] = sampling
    if frozen and "model_id" not in changed and any((model or {}).get(key) != (frozen.get("model") or {}).get(key) for key in ("name", "endpoint", "api")):
        raise ProfileError("Recorded model connection changed; create a run with new conditions")
    limits = {"wall_time_seconds": int(getattr(template, "timeout_minutes", 60) or 60) * 60,
              "memory_mb": memory_mb(getattr(template, "max_ram", "2g")),
              "cpu_cores": getattr(template, "max_cpu", 100000) / 100000}
    if overrides.get("max_iterations") is not None:
        limits["max_turns"] = overrides["max_iterations"]
    spec = {
        "protocol_version": "1.0", "workspace_id": str(task.workspace_id), "task_id": str(task.id),
        "attempt_id": str(attempt_id), "task": {"title": task.title, "prompt": prompt}, "cwd": "/workspace",
        "instructions": [instructions] if instructions else [], "config": deepcopy(profile.config),
        "model": model, "credential_refs": {key: "runtime-injected" for key in credentials},
        "limits": limits, "environment": environment,
        "metadata": {"execution_mode": mode, "profile_id": str(profile.id), "model_id": str(llm.model.id) if llm is not None else None,
                     "template_name": template.name if template else None,
                     "template_updated_at": template.updated_at.isoformat() if template and template.updated_at else None,
                     "template_id": str(template.id) if template else None},
    }
    if frozen:
        from app.harnesses.provenance import digest

        if frozen.get("configuration_state") != "prepared" or not frozen.get("image_id"):
            raise ProfileError("Recorded applied configuration or image is unknown; create a run with new conditions")
        if frozen.get("manifest_sha256") != digest(manifest):
            raise ProfileError("Installed connector declaration changed; create a run with new conditions")
        prior_environment = deepcopy(frozen.get("environment") or {})
        if prior_environment.get("id") != "workspace":
            raise ProfileError("This environment requires a fresh prepared run rather than direct replay")
        prior_resources = prior_environment.get("resources", [])
        current_mcp = [item for item in declarations if item["kind"] == "mcp" and item["id"] != "evalhive_knowledge"]
        prior_mcp = [item for item in prior_resources if item["kind"] == "mcp" and item["id"] != "evalhive_knowledge"]
        if current_mcp != prior_mcp and "tools_override" not in changed:
            raise ProfileError("Recorded MCP resources changed; create a run with new conditions")
        for item in prior_resources:
            if item["id"] == "evalhive_knowledge":
                from app.harnesses.resources import knowledge_resource

                item.update(knowledge_resource(attempt_id))
        if "tools_override" in changed:
            prior_resources[:] = [item for item in prior_resources if item["kind"] not in {"tools", "mcp"}]
            prior_resources.extend(item for item in declarations if item["kind"] in {"tools", "mcp"})
        if "memory_mode" in changed:
            prior_resources[:] = [item for item in prior_resources if item["kind"] != "memory"]
            prior_resources.extend(item for item in declarations if item["kind"] == "memory")
        known_roles = set(credentials) | {"evalhive_knowledge_token"}
        if set(frozen.get("credential_roles", [])) - known_roles:
            raise ProfileError("Recorded private credential references are unavailable")
        spec["config"] = {**deepcopy(frozen["config"]), **deepcopy(overrides.get("harness_config", {}))}
        if frozen.get("task") and not overrides.get("replay_input_override"):
            spec["task"] = deepcopy(frozen["task"])
        spec["instructions"] = deepcopy(frozen.get("instructions", []))
        if str(getattr(template, "id", "")) == frozen.get("template_id"):
            spec["metadata"]["template_updated_at"] = frozen.get("template_updated_at")
            spec["metadata"]["template_name"] = frozen.get("template_name")
        if "template_id" in changed and str(getattr(template, "id", "")) != frozen.get("template_id"):
            spec["instructions"] = [instructions] if instructions else []
        if overrides.get("soul_md") is not None:
            spec["instructions"] = [overrides["soul_md"]]
        spec["environment"] = prior_environment
        spec["limits"] = deepcopy(frozen.get("limits", limits))
        if "max_iterations" in changed:
            spec["limits"]["max_turns"] = overrides["max_iterations"]
        spec["metadata"]["expected_image_id"] = frozen["image_id"]
        spec["metadata"]["replay_conditions"] = "recorded_public_settings"
    else:
        spec["config"].update(deepcopy(overrides.get("harness_config", {})))
        if overrides.get("new_harness_conditions"):
            spec["metadata"]["replay_conditions"] = "new_conditions"
    validate_run(manifest, spec)
    return ResolvedLaunch(manifest, spec, credentials, profile, network, llm.model if llm is not None else None)


async def launch_task(db, task, template=None):
    resolved = await resolve_launch(db, task, template)
    task.model_used = resolved.model.api_name if resolved.model else None
    task.input_price_per_1m_usd = resolved.model.input_price_per_1m_usd if resolved.model else None
    task.output_price_per_1m_usd = resolved.model.output_price_per_1m_usd if resolved.model else None
    return await launch_attempt(
        db, task=task, manifest=resolved.manifest, run_spec=resolved.spec,
        credentials=resolved.credentials, profile_id=resolved.profile.id, network=resolved.network,
    )
