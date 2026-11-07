"""Resolve provider-owned installation data into immutable rootless grants.

Benchmark workers select installed asset IDs. They cannot supply host paths,
images, quotas, process startup or output permissions in a resource request.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from evalhive_benchmark import ProtocolError, validate
from evalhive_benchmark.peer import identity
from evalhive_benchmark.protocol import _json_value
from jsonschema import Draft202012Validator

from app.runtime.admission import ResourceDemand, StorageDemand
from app.runtime.broker import ExportGrant, ResourceLease
from app.runtime.managed_spec import (
    ContainerSpec,
    FinalizerSpec,
    ManagedRuntimeError,
    NetworkRequest,
    Quota,
    VolumeRequest,
    absolute_path,
)
from app.runtime.spec import BindMount

ID = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$"}
TEXT = {"type": "string", "minLength": 1, "maxLength": 4096, "pattern": "^[^\\u0000]*$"}
ARGV = {"type": ["array", "null"], "maxItems": 64, "items": TEXT}


def obj(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


OUTPUT = obj(
    {
        "path": TEXT,
        "visibility": {"enum": ["user", "evaluator", "private"]},
        "max_archive_bytes": {"type": "integer", "minimum": 1, "maximum": 2**31},
        "required": {"type": "boolean"},
        "rootfs": {"type": "boolean"},
        "stage": {"enum": ["capture", "evaluation"]},
    },
    ("path", "visibility", "max_archive_bytes", "required"),
)
CONTAINER = obj(
    {
        "kind": {"const": "container"},
        "image": {
            "type": "string",
            "maxLength": 4096,
            "pattern": "^(sha256:[a-f0-9]{64}|[A-Za-z0-9][^\\s]*@sha256:[a-f0-9]{64})$",
        },
        "role": {"enum": ["worker", "environment", "service", "grader"]},
        "limits": obj(
            {
                "memory_mb": {"type": "integer", "minimum": 6},
                "cpu_cores": {"type": "number", "exclusiveMinimum": 0},
                "pids": {"type": "integer", "minimum": 1},
            },
            ("memory_mb", "cpu_cores", "pids"),
        ),
        "minimum": obj(
            {
                "memory_mb": {"type": "integer", "minimum": 1},
                "cpu_cores": {"type": "number", "exclusiveMinimum": 0},
                "disk_mb": {"type": "integer", "minimum": 0},
            },
            ("memory_mb", "cpu_cores", "disk_mb"),
        ),
        "entrypoint": ARGV,
        "command": ARGV,
        "workdir": TEXT,
        "user": TEXT,
        "capabilities": {
            "type": "array",
            "maxItems": 14,
            "uniqueItems": True,
            "items": {"type": "string", "maxLength": 64},
        },
        "agent_process_policy": {"enum": ["stop_after_agent", "retain_until_cleanup"]},
        "agent_user": TEXT,
        "finalizer": obj(
            {
                "argv": ARGV,
                "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 600},
                "replay_safe": {"const": True},
            },
            ("argv", "timeout_seconds", "replay_safe"),
        ),
        "environment": {
            "type": "object",
            "maxProperties": 128,
            "additionalProperties": {"type": "string", "maxLength": 32768},
        },
        "mounts": {
            "type": "array",
            "maxItems": 64,
            "items": obj(
                {"file_id": ID, "target": TEXT, "read_only": {"type": "boolean"}},
                ("file_id", "target", "read_only"),
            ),
        },
        "readonly_root": {"type": "boolean"},
        "storage_slots": {
            "type": "object",
            "maxProperties": 16,
            "propertyNames": ID,
            "additionalProperties": obj(
                {"target": TEXT, "read_only": {"type": "boolean"}, "no_copy": {"type": "boolean"}},
                ("target", "read_only"),
            ),
        },
        "outputs": {
            "type": "object",
            "maxProperties": 256,
            "propertyNames": ID,
            "additionalProperties": OUTPUT,
        },
    },
    ("kind", "image", "role", "limits", "minimum"),
)
SCHEMA = obj(
    {
        "format_version": {"const": "1.0"},
        "connector_id": ID,
        "connector_version": ID,
        "provider_id": ID,
        "worker_asset": ID,
        "assets": {
            "type": "object",
            "minProperties": 1,
            "maxProperties": 4096,
            "propertyNames": ID,
            "additionalProperties": {
                "oneOf": [
                    CONTAINER,
                    obj(
                        {
                            "kind": {"const": "volume"},
                            "disk_mb": {"type": "integer", "minimum": 1, "maximum": 2**53 - 1},
                        },
                        ("kind", "disk_mb"),
                    ),
                    obj(
                        {"kind": {"const": "network"}, "egress": {"type": "boolean"}},
                        ("kind", "egress"),
                    ),
                ]
            },
        },
    },
    (
        "format_version",
        "connector_id",
        "connector_version",
        "provider_id",
        "worker_asset",
        "assets",
    ),
)


@dataclass(frozen=True)
class RegisteredFile:
    host_path: str
    source_digest: str
    writable: bool = False

    def __post_init__(self):
        absolute_path(self.host_path)
        if (
            not isinstance(self.source_digest, str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", self.source_digest)
            or type(self.writable) is not bool
        ):
            raise ManagedRuntimeError("Registered files require a verified installation digest")


@dataclass(frozen=True)
class CompiledEnvironment:
    scope: object
    leases: dict
    demands: tuple[ResourceDemand | StorageDemand, ...]
    outputs: dict
    links: dict
    fingerprint: str
    images: dict
    files: dict

    def output_grants(self, actor_ids):
        return {
            resource: {
                output: {"actors": [str(actor) for actor in actor_ids], "grant": asdict(grant)}
                for output, grant in grants.items()
            }
            for resource, grants in self.outputs.items()
        }


class RootlessAssets:
    def __init__(self, document, runtime, *, files=None, connector_id, connector_version):
        try:
            _json_value(document)
            if len(json.dumps(document).encode()) > 1024**2 or not Draft202012Validator(
                SCHEMA
            ).is_valid(document):
                raise ValueError
            if (document["connector_id"], document["connector_version"]) != (
                connector_id,
                connector_version,
            ):
                raise ValueError
            worker = document["assets"][document["worker_asset"]]
            if worker["kind"] != "container" or worker["role"] != "worker":
                raise ValueError
        except (KeyError, TypeError, ValueError, RecursionError, ProtocolError):
            raise ManagedRuntimeError("Invalid registered rootless assets") from None
        self.document, self.runtime, self.files = deepcopy(document), runtime, dict(files or {})
        self.provider_id, self.worker_asset = document["provider_id"], document["worker_asset"]

    @classmethod
    def load(cls, path, runtime, **options):
        path = Path(path).absolute()
        try:
            if (
                path.resolve(strict=True) != path
                or not path.is_file()
                or path.stat().st_size > 1024**2
            ):
                raise ValueError
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            raise ManagedRuntimeError("Registered resource document is unavailable") from None
        return cls(document, runtime, **options)

    async def resource(self, scope, logical_id, asset_id, *, kind=None):
        asset = self.document["assets"].get(asset_id) if isinstance(asset_id, str) else None
        if asset is None or kind is not None and asset["kind"] != kind:
            raise ProtocolError(
                "ASSET_UNAVAILABLE", "Installed resource asset is unavailable", "preparation"
            )
        handle = str(uuid.uuid5(uuid.UUID(scope.session_id), "resource:" + logical_id))
        if asset["kind"] == "network":
            return (
                ResourceLease(
                    handle, scope, "network", NetworkRequest(scope, handle, asset["egress"])
                ),
                None,
                None,
            )
        if asset["kind"] == "volume":
            return (
                ResourceLease(
                    handle, scope, "volume", VolumeRequest(scope, handle, asset["disk_mb"])
                ),
                StorageDemand(logical_id, asset["disk_mb"]),
                None,
            )
        quota = Quota(**asset["limits"])
        quota.validate(self.runtime.policy)
        minimum = asset["minimum"]
        if quota.memory_mb < minimum["memory_mb"] or quota.cpu_cores < minimum["cpu_cores"]:
            raise ManagedRuntimeError("Registered limits lower native resource requirements")
        mounts = []
        for mount in asset.get("mounts", []):
            source = self.files.get(mount["file_id"])
            if source is None or not mount["read_only"] and not source.writable:
                raise ManagedRuntimeError("Registered mount is unavailable or not writable")
            absolute_path(source.host_path)
            if absolute_path(mount["target"]).as_posix() == "/":
                raise ManagedRuntimeError("Registered mount cannot obscure the root filesystem")
            mounts.append(BindMount(source.host_path, mount["target"], mount["read_only"]))
        image = await asyncio.to_thread(self.runtime.resolve_image, asset["image"])
        spec = ContainerSpec(
            scope,
            handle,
            image["image_id"],
            role=asset["role"],
            entrypoint=None if asset.get("entrypoint") is None else tuple(asset["entrypoint"]),
            command=None if asset.get("command") is None else tuple(asset["command"]),
            workdir=asset.get("workdir"),
            user=asset.get("user"),
            environment=deepcopy(asset.get("environment", {})),
            mounts=tuple(mounts),
            quota=quota,
            readonly_root=asset.get("readonly_root", False),
            capabilities=tuple(asset.get("capabilities", [])),
            finalizer=FinalizerSpec(
                tuple(asset["finalizer"]["argv"]),
                asset["finalizer"]["timeout_seconds"],
                asset["finalizer"]["replay_safe"],
            )
            if "finalizer" in asset
            else None,
        )
        if not set(spec.capabilities) <= set(self.runtime.policy.allowed_capabilities):
            raise ManagedRuntimeError("Native Linux capabilities exceed runtime policy")
        if spec.workdir is not None:
            absolute_path(spec.workdir)
        demand = ResourceDemand(
            logical_id, quota, minimum["memory_mb"], minimum["cpu_cores"], minimum["disk_mb"]
        )
        return ResourceLease(handle, scope, "container", spec), demand, image["image_id"]

    async def compile(self, scope, plan, *, reserved_ids=(), retained_inputs=None):
        validate("environment_plan", plan)
        retained_inputs = dict(retained_inputs or {})
        resources = {item["id"]: item for item in plan["resources"]}
        if set(resources) & set(reserved_ids):
            raise ManagedRuntimeError("Plan overlaps an already reserved worker resource")
        leases, demands, images, links, outputs = {}, [], {}, {}, {}
        for name, resource in resources.items():
            config = resource["config"]
            if resource["provider"] != self.provider_id or set(config) - {
                "asset_id",
                "network",
                "namespace",
                "network_aliases",
                "retained_inputs",
                "storage",
            }:
                raise ManagedRuntimeError(
                    "Plan requires an unregistered provider or resource setting"
                )
            asset_id = config.get("asset_id")
            lease, demand, image = await self.resource(scope, name, asset_id, kind=resource["kind"])
            asset = self.document["assets"][asset_id]
            if asset["kind"] == "container" and asset["role"] not in {
                "environment",
                "service",
                "grader",
            }:
                raise ManagedRuntimeError("A task plan cannot install another controller worker")
            selected = {
                field: config[field] for field in ("network", "namespace") if field in config
            }
            if len(selected) > 1 or any(
                target not in resource.get("depends_on", []) for target in selected.values()
            ):
                raise ManagedRuntimeError("Native network links require declared dependencies")
            for field, target in selected.items():
                if (field == "network" and resources[target]["kind"] != "network") or (
                    field == "namespace" and resources[target]["kind"] != "container"
                ):
                    raise ManagedRuntimeError("Native network dependency has the wrong kind")
            if selected and lease.kind != "container":
                raise ManagedRuntimeError("Only containers can join registered native networks")
            storage = config.get("storage", {})
            slots = asset.get("storage_slots", {})
            if (
                not isinstance(storage, dict)
                or set(storage) != set(slots)
                or storage
                and lease.kind != "container"
            ):
                raise ManagedRuntimeError("Storage bindings must match registered container slots")
            bindings = []
            for slot, target in storage.items():
                if (
                    not isinstance(target, str)
                    or target not in resources
                    or target not in resource.get("depends_on", [])
                    or resources[target]["kind"] != "volume"
                ):
                    raise ManagedRuntimeError("Storage requires a declared owned volume dependency")
                path = absolute_path(slots[slot]["target"])
                if str(path) == "/":
                    raise ManagedRuntimeError("Storage cannot obscure the root filesystem")
                bindings.append(
                    {
                        "resource": target,
                        "target": str(path),
                        "read_only": slots[slot]["read_only"],
                        **({"no_copy": slots[slot]["no_copy"]} if "no_copy" in slots[slot] else {}),
                    }
                )
            if bindings:
                selected["storage"] = bindings
            aliases = config.get("network_aliases", [])
            if (
                not isinstance(aliases, list)
                or len(aliases) > 16
                or any(
                    not isinstance(value, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", value)
                    for value in aliases
                )
                or len(set(aliases)) != len(aliases)
                or "network_aliases" in config
                and "network" not in selected
            ):
                raise ManagedRuntimeError("Network aliases require an owned bridge")
            if lease.kind == "container":
                lease = replace(lease, intent=replace(lease.intent, network_aliases=tuple(aliases)))
            selected_inputs = config.get("retained_inputs", [])
            if (
                not isinstance(selected_inputs, list)
                or len(selected_inputs) > 256
                or any(
                    not isinstance(value, str) or value not in retained_inputs
                    for value in selected_inputs
                )
                or len(set(selected_inputs)) != len(selected_inputs)
                or selected_inputs
                and lease.kind != "container"
            ):
                raise ManagedRuntimeError("Retained inputs require distinct authorized file grants")
            if selected_inputs:
                mounts = list(lease.intent.mounts)
                for input_id in selected_inputs:
                    source = retained_inputs[input_id]
                    if not re.fullmatch(ID["pattern"], input_id) or source.writable:
                        raise ManagedRuntimeError("Retained input grants must be read-only")
                    target = "/evalhive/retained/" + input_id + "/payload"
                    for mount in mounts:
                        left, right = absolute_path(mount.source), absolute_path(source.host_path)
                        if left.is_relative_to(right) or right.is_relative_to(left):
                            raise ManagedRuntimeError("Retained inputs overlap another host mount")
                        left, right = absolute_path(mount.target), absolute_path(target)
                        if left.is_relative_to(right) or right.is_relative_to(left):
                            raise ManagedRuntimeError(
                                "Retained inputs overlap another container mount"
                            )
                    mounts.append(BindMount(source.host_path, target, True))
                lease = replace(lease, intent=replace(lease.intent, mounts=tuple(mounts)))
            dependencies = tuple(
                str(uuid.uuid5(uuid.UUID(scope.session_id), "resource:" + dependency))
                for dependency in resource.get("depends_on", [])
            )
            leases[name] = (self.provider_id, replace(lease, dependencies=dependencies))
            links[name] = selected
            if demand:
                demands.append(demand)
            if image is not None:
                images[name] = image
            declared_outputs = {
                item["id"]: item for item in plan["outputs"] if item["resource_id"] == name
            }
            for output_id, rule in asset.get("outputs", {}).items():
                if rule["required"] and output_id not in declared_outputs:
                    raise ManagedRuntimeError("Plan omits a required registered output")
            for output_id, declaration in declared_outputs.items():
                rule = asset.get("outputs", {}).get(output_id)
                if (
                    rule is None
                    or any(
                        declaration[key] != rule[key] for key in ("path", "visibility", "required")
                    )
                    or declaration.get("stage", "capture") != rule.get("stage", "capture")
                ):
                    raise ManagedRuntimeError("Plan output differs from its registered grant")
                path = absolute_path(rule["path"])
                if (rule.get("rootfs") or len(path.parts) == 1) and rule["visibility"] != "private":
                    raise ManagedRuntimeError("Root filesystem exports must remain private")
                outputs.setdefault(name, {})[output_id] = ExportGrant(
                    output_id,
                    rule["path"],
                    rule["visibility"],
                    rule["max_archive_bytes"],
                    rule.get("rootfs", False),
                )
        agent = plan.get("agent")
        if agent is not None:
            if agent["mode"] == "in_environment":
                selected = leases.get(agent.get("environment_handle"))
                if (
                    selected is None
                    or selected[1].kind != "container"
                    or selected[1].intent.role != "environment"
                ):
                    raise ManagedRuntimeError("Agent requires a declared task container")
                task_resource = resources[agent["environment_handle"]]
                task_asset = self.document["assets"][task_resource["config"]["asset_id"]]
                if agent.get("process_policy", "stop_after_agent") != task_asset.get(
                    "agent_process_policy", "stop_after_agent"
                ):
                    raise ManagedRuntimeError(
                        "Agent process policy differs from its registered task"
                    )
                if agent.get("user") != task_asset.get("agent_user"):
                    raise ManagedRuntimeError("Agent user differs from its registered task")
            absolute_path(agent["cwd"])
        files = {
            mount["file_id"]: self.files[mount["file_id"]].source_digest
            for resource in resources.values()
            for mount in self.document["assets"][resource["config"]["asset_id"]].get("mounts", [])
        }
        files.update(
            {
                "retained/" + input_id: retained_inputs[input_id].source_digest
                for resource in resources.values()
                for input_id in resource["config"].get("retained_inputs", [])
            }
        )
        fingerprint = identity(
            {"registration": self.document, "images": images, "files": files, "plan": plan}
        )
        return CompiledEnvironment(
            scope, leases, tuple(demands), outputs, links, fingerprint, images, files
        )
