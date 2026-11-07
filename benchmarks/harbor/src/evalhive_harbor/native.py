"""Use the pinned native parser while rejecting silently discarded task settings."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tomllib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from harbor import __version__
from harbor.constants import MAIN_SERVICE_NAME
from harbor.models.task.artifacts import normalize_artifact_entries
from harbor.models.task.config import TaskConfig
from harbor.models.task.task import Task
from harbor.models.task.verifier_mode import (
    resolve_task_verifier_mode,
    resolve_verifier_environment_definition,
)
from jsonschema import Draft202012Validator

from .policies import network_plan, network_requirements

NATIVE_VERSION = "0.25.0"
NATIVE_REVISION = "fdba28fc411e47e8765420a1a063b4918fce3def"


class HarborConfigurationError(ValueError):
    pass


def strict_native_schema():
    schema = deepcopy(TaskConfig.model_json_schema())

    def visit(value):
        if isinstance(value, dict):
            if value.get("type") == "object" and "properties" in value:
                value.setdefault("additionalProperties", False)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    # Harbor explicitly migrates these legacy inputs before native validation.
    schema["properties"]["version"] = {"type": "string"}
    for name in ("memory", "storage"):
        schema["$defs"]["EnvironmentConfig"]["properties"][name] = {"type": "string"}
    schema["$defs"]["MCPServerConfig"]["properties"]["transport"]["enum"].append("http")
    return schema


def source_digest(root, *, max_bytes=2 * 1024**3, max_entries=65536):
    root = Path(root)
    digest, entries, size = hashlib.sha256(b"evalhive-harbor-task-v1\n"), 0, 0

    def observe(path, depth=0):
        nonlocal entries, size
        entries += 1
        if entries > max_entries or depth > 128:
            raise HarborConfigurationError("Native task inventory exceeds its entry limit")
        info = path.lstat()
        record = {"path": path.relative_to(root).as_posix(), "mode": info.st_mode & 0o777}
        if stat.S_ISDIR(info.st_mode):
            record["kind"] = "directory"
            digest.update((json.dumps(record, sort_keys=True) + "\n").encode())
            children = []
            for child in path.iterdir():
                children.append(child)
                if entries + len(children) > max_entries:
                    raise HarborConfigurationError("Native task inventory exceeds its entry limit")
            for child in sorted(children):
                observe(child, depth + 1)
        elif stat.S_ISREG(info.st_mode):
            size += info.st_size
            if size > max_bytes:
                raise HarborConfigurationError("Native task inventory exceeds its byte limit")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise HarborConfigurationError("Native task file changed its type")
                content, counted = hashlib.sha256(), 0
                while chunk := stream.read(128 * 1024):
                    counted += len(chunk)
                    if counted > info.st_size:
                        raise HarborConfigurationError("Native task changed during inventory")
                    content.update(chunk)
            if counted != info.st_size:
                raise HarborConfigurationError("Native task changed during inventory")
            record.update(kind="file", size_bytes=counted, digest=content.hexdigest())
            digest.update((json.dumps(record, sort_keys=True) + "\n").encode())
        else:
            raise HarborConfigurationError("Native task requires unsupported filesystem entries")
        after = path.lstat()
        if any(
            getattr(info, name) != getattr(after, name)
            for name in ("st_ino", "st_dev", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        ):
            raise HarborConfigurationError("Native task changed during inventory")

    if root.resolve(strict=True) != root:
        raise HarborConfigurationError("Native task root cannot traverse links")
    observe(root)
    return "sha256:" + digest.hexdigest()


@dataclass(frozen=True)
class NativeTask:
    task: Task
    raw_config: dict
    source_digest: str

    @property
    def verifier_mode(self):
        return resolve_task_verifier_mode(self.task.config).value

    def requirements(self):
        environment = self.task.config.environment
        resources = {}
        for source, target in (
            ("cpus", "cpu_cores"),
            ("memory_mb", "memory_mb"),
            ("storage_mb", "disk_mb"),
        ):
            value = getattr(environment, source)
            if value is not None:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (float, int))
                    or not math.isfinite(value)
                    or value <= 0
                ):
                    raise HarborConfigurationError("Native resource requirement must be positive")
                resources[target] = value
        capabilities = ["host.agent_api_v1"]
        if environment.gpus or environment.gpu_types:
            capabilities.append("hardware.gpu")
        if environment.tpu is not None:
            capabilities.append("hardware.tpu")
        if self.task.config.steps:
            capabilities.append("harbor.multi_step_v1")
        capabilities.extend(network_requirements(self.task))
        if environment.mcp_servers:
            capabilities.append("harbor.native_mcp_v1")
        if environment.skills_dir:
            capabilities.append("harbor.native_skills_v1")
        if (self.task.paths.environment_dir / "docker-compose.yaml").exists():
            capabilities.append("harbor.compose_v1")
        artifacts = normalize_artifact_entries(self.task.config.artifacts)
        if any(artifact.exclude for artifact in artifacts):
            capabilities.append("harbor.artifact_excludes_v1")
        if any(artifact.service not in {None, MAIN_SERVICE_NAME} for artifact in artifacts) or any(
            hook.service != MAIN_SERVICE_NAME for hook in self.task.config.verifier.collect
        ):
            capabilities.append("harbor.sidecars_v1")
        budget = self.task.config.agent.timeout_sec
        if budget is not None and (not float(budget).is_integer() or not 1 <= budget <= 86400):
            capabilities.append("agent.native_deadline_adapter")
        if any(
            "${" in value
            for value in [*environment.env.values(), *self.task.config.verifier.env.values()]
        ):
            capabilities.append("environment.host_bindings_v1")
        return {
            "resources": resources,
            "capabilities": sorted(capabilities),
            "platform": {"os": environment.os.value},
        }

    def evaluation_requirements(self):
        definition = resolve_verifier_environment_definition(self.task.config, self.task.paths)
        if definition is None:
            return None
        environment = definition.config
        resources = {}
        for source, target in (
            ("cpus", "cpu_cores"),
            ("memory_mb", "memory_mb"),
            ("storage_mb", "disk_mb"),
        ):
            value = getattr(environment, source)
            if value is not None:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value <= 0
                ):
                    raise HarborConfigurationError(
                        "Native verifier resources must be positive and finite"
                    )
                resources[target] = value
        capabilities = []
        if environment.gpus or environment.gpu_types:
            capabilities.append("hardware.gpu")
        if environment.tpu is not None:
            capabilities.append("hardware.tpu")
        policies = network_plan(self.task)
        if policies.verifier_phase.network_mode.value == "allowlist":
            capabilities.append("network.allowlist")
        if policies.verifier_phase_baseline != policies.verifier_phase:
            capabilities.append("network.dynamic_policy")
        if (definition.directory / "docker-compose.yaml").exists():
            capabilities.append("harbor.compose_v1")
        if any(
            "${" in value
            for value in [*environment.env.values(), *self.task.config.verifier.env.values()]
        ):
            capabilities.append("environment.host_bindings_v1")
        return {
            "resources": resources,
            "capabilities": sorted(set(capabilities)),
            "platform": {"os": environment.os.value},
        }


def parse_task(root):
    if __version__ != NATIVE_VERSION:
        raise HarborConfigurationError("Installed Harbor version differs from the connector pin")
    root = Path(root).absolute()
    proof = source_digest(root)
    path = root / "task.toml"
    if path.stat().st_size > 1024**2:
        raise HarborConfigurationError("Native task configuration exceeds its byte limit")
    try:
        raw = tomllib.loads(path.read_text())
        if not Draft202012Validator(strict_native_schema()).is_valid(raw):
            raise HarborConfigurationError(
                "Native task has unknown or unsupported configuration fields"
            )
        if "version" in raw and "schema_version" in raw and raw["version"] != raw["schema_version"]:
            raise HarborConfigurationError("Native schema version declarations conflict")
        native = Task(root)
        result = NativeTask(native, raw, proof)
        result.requirements()
        result.evaluation_requirements()
        return result
    except HarborConfigurationError:
        raise
    except (ValueError, OSError, KeyError, TypeError):
        raise HarborConfigurationError("Native Harbor task could not be parsed") from None
