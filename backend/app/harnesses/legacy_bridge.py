"""Temporary conversion from AgentSpec; no native harness modules are imported."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path, PurePosixPath

from app.config import get_settings
from app.harnesses.catalog import get_manifest
from app.harnesses.executor import launch_attempt
from app.runtime.spec import BindMount, NetworkSpec

ENVIRONMENT_OPTIONS = {
    "AGENT_TOOL_INJECTION": ("tool_injection", str),
    "LLM_TEMPERATURE": ("temperature", float),
    "LLM_SEED": ("seed", int),
    "AGENT_BENCHMARK_MODE": ("benchmark_mode", lambda value: value == "1"),
    "PGHOST": ("pg_host", str),
    "PG_HOST": ("pg_host", str),
}


def convert_spec(task, spec, *, timeout_seconds):
    identity = "legacy"
    if spec.image is not None:
        if spec.image != "evalhive-agent-toolathlon:latest":
            raise ValueError("Legacy image is not registered as an SDK connector")
        identity = "legacy-toolathlon"
    manifest = get_manifest(identity)
    credentials = {"model_api_key": spec.env.get("OPENAI_API_KEY", "")}
    if spec.agent_token:
        credentials["knowledge_token"] = spec.agent_token
    config = {"tools": spec.tools, "memory_context": spec.memory_context}
    mcp_refs = []
    for index, server in enumerate(spec.mcp_servers):
        reference = f"legacy_mcp_{index}"
        credentials[reference] = json.dumps(server, ensure_ascii=False, allow_nan=False)
        mcp_refs.append(reference)
    config["mcp_credential_refs"] = mcp_refs
    limits = {"wall_time_seconds": timeout_seconds}
    for name, value in spec.extra_env.items():
        if name == "AGENT_MAX_ITERATIONS":
            limits["max_turns"] = int(value)
        elif name in ENVIRONMENT_OPTIONS:
            key, convert = ENVIRONMENT_OPTIONS[name]
            config[key] = convert(value)
        else:
            raise ValueError("Legacy environment option is not declared")
    if memory := re.fullmatch(
        r"([1-9][0-9]*)([kmgt]?)", (spec.resource_limits.get("max_ram") or "2g").lower()
    ):
        byte_count = (
            int(memory[1]) * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[memory[2]]
        )
        if byte_count % (1024**2):
            raise ValueError("Legacy memory limit must be a whole number of MiB")
        limits["memory_mb"] = byte_count // (1024**2)
    else:
        raise ValueError("Legacy memory limit is invalid")
    limits["cpu_cores"] = (spec.resource_limits.get("max_cpu") or 100000) / 100000
    run_spec = {
        "protocol_version": "1.0",
        "workspace_id": str(task.workspace_id),
        "task_id": str(task.id),
        "attempt_id": str(uuid.uuid4()),
        "task": {"title": task.title, "prompt": spec.task_description},
        "cwd": "/workspace",
        "instructions": [spec.soul_md],
        "config": config,
        "model": {
            "name": spec.env.get("LLM_MODEL", ""),
            "endpoint": spec.env.get("OPENAI_BASE_URL", ""),
            "api": "openai-compatible",
            "credential_ref": "model_api_key",
        },
        "credential_refs": {key: "runtime-injected" for key in credentials},
        "limits": limits,
        "environment": {
            "id": "toolathlon" if identity.endswith("toolathlon") else "workspace",
            "artifact_policy": {"root": "." if config.get("benchmark_mode") else "output"},
        },
        "metadata": {"transition": "legacy", "template_id": spec.template_id},
    }
    return manifest, run_spec, credentials


async def launch_legacy(db, task, spec, *, timeout_seconds):
    manifest, run_spec, credentials = convert_spec(task, spec, timeout_seconds=timeout_seconds)
    settings = get_settings()
    extra_mounts = []
    memory_mode = spec.memory_mode
    for name in ("rules.md", "memory.md"):
        if name == "memory.md" and memory_mode == "off":
            continue
        source = Path(settings.data_dir) / "shared" / str(task.workspace_id) / name
        if source.is_symlink():
            raise ValueError("Legacy shared resource cannot be a symlink")
        if source.is_file():
            host = PurePosixPath(settings.host_data_dir) / "shared" / str(task.workspace_id) / name
            extra_mounts.append(BindMount(str(host), "/data/" + name))
    network = NetworkSpec()
    if spec.network_mode:
        if not spec.network_mode.startswith("container:"):
            raise ValueError("Legacy network mode is unsupported")
        network = NetworkSpec(container=spec.network_mode.removeprefix("container:"))
    return await launch_attempt(
        db,
        task=task,
        manifest=manifest,
        run_spec=run_spec,
        credentials=credentials,
        network=network,
        extra_mounts=extra_mounts,
    )
