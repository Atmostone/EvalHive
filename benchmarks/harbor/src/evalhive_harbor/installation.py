"""Publish an immutable native dataset and declarative rootless installation."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from copy import deepcopy
from pathlib import Path, PurePosixPath

from evalhive_benchmark.installation import dataset_key
from harbor.models.task.verifier_mode import resolve_verifier_environment_definition

from .archive import digest_file
from .compose import read_compose
from .dataset import CONNECTOR_ID, CONNECTOR_VERSION, InstalledHarbor, build_receipt
from .native import HarborConfigurationError
from .transfers import absolute_path

TRANSFER_TOOLS = json.loads(Path(__file__).with_name("tools.lock.json").read_text())["files"]


def image_pin(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:sha256:|[^\s]+@sha256:)[a-f0-9]{64}", value
    ):
        raise HarborConfigurationError("Native registration requires immutable image identities")
    return value


def write_json(path, value):
    with Path(path).open("xb") as stream:
        stream.write(
            (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()
        )
        stream.flush()
        os.fsync(stream.fileno())


def container(image, role, memory, cpu, disk, **settings):
    return {
        "kind": "container",
        "role": role,
        "image": image_pin(image),
        "limits": {"memory_mb": memory, "cpu_cores": cpu, "pids": 256},
        "minimum": {"memory_mb": memory, "cpu_cores": cpu, "disk_mb": disk},
        **settings,
    }


def image_limits(selected):
    """Validate only administrator-inspected metadata, never execute an image."""
    if set(selected) - {"image", "image_user", "image_workdir", "limits", "reserved_paths_checked"}:
        raise HarborConfigurationError("Native image binding has unknown settings")
    if selected.get("reserved_paths_checked") is not True:
        raise HarborConfigurationError("Native image control paths were not inspected")
    limits = selected["limits"]
    if set(limits) != {"memory_mb", "cpu_cores", "disk_mb"}:
        raise HarborConfigurationError("Native image limits must be explicit")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        for value in limits.values()
    ):
        raise HarborConfigurationError("Native image limits must be positive and finite")
    if type(limits["memory_mb"]) is not int or type(limits["disk_mb"]) is not int:
        raise HarborConfigurationError("Native memory and disk budgets must be whole MiB")
    image_pin(selected["image"])
    return limits


def native_asset(native, selected, *, verifier=False, compose=None):
    """Selected metadata comes from the administrator's exact image inspection."""
    limits = image_limits(selected)
    config = (
        resolve_verifier_environment_definition(native.task.config, native.task.paths).config
        if verifier
        else native.task.config.environment
    )
    if (
        compose is None
        and (
            native.task.paths.task_dir
            / ("tests" if verifier else "environment")
            / "docker-compose.yaml"
        ).exists()
    ):
        raise HarborConfigurationError("Native Compose requires installed service bindings")
    if any("${" in value for value in config.env.values()):
        raise HarborConfigurationError(
            "Native environment templates require explicit private bindings"
        )
    for name, required in (
        ("memory_mb", config.memory_mb),
        ("cpu_cores", config.cpus),
        ("disk_mb", config.storage_mb),
    ):
        if required is not None and limits[name] < required:
            raise HarborConfigurationError("Installed limits lower native task requirements")
    cwd = absolute_path(config.workdir or selected.get("image_workdir") or "/")
    user = selected.get("image_user")
    storage = {
        "stage": {"target": "/evalhive-stage", "read_only": True},
        "verifier-logs": {"target": "/logs/verifier", "read_only": False},
        "agent-artifacts": {"target": "/logs/artifacts", "read_only": False},
        "agent-logs": {"target": "/logs/agent", "read_only": False},
    }
    result = container(
        selected["image"],
        "grader" if verifier else "environment",
        limits["memory_mb"],
        limits["cpu_cores"],
        limits["disk_mb"],
        command=["sh", "-c", "sleep infinity"],
        workdir=cwd,
        environment=deepcopy(config.env),
        **({"user": user} if user else {}),
        storage_slots=storage,
        mounts=[
            {"file_id": "transfer-tools", "target": "/evalhive-transfer", "read_only": True},
            {"file_id": "harness-runtime", "target": "/evalhive-runtime", "read_only": True},
        ],
    )
    if not verifier:
        result["agent_process_policy"] = "retain_until_cleanup"
        if native.task.config.agent.user is not None:
            result["agent_user"] = str(native.task.config.agent.user)
        result["outputs"] = {
            "task-artifacts": {
                "path": "/logs/artifacts",
                "visibility": "user",
                "required": False,
                "max_archive_bytes": 64 * 1024**2,
            }
        }
    return result, cwd


def compose_asset(selected, description, *, original=None):
    limits = image_limits(selected)
    for name, minimum in description.get("native_limits", {}).items():
        if limits[name] < minimum:
            raise HarborConfigurationError("Installed limits lower native Compose requirements")
    result = original or container(
        selected["image"],
        "service",
        limits["memory_mb"],
        limits["cpu_cores"],
        limits["disk_mb"],
        workdir=selected.get("image_workdir") or "/",
        **({"user": selected["image_user"]} if selected.get("image_user") else {}),
        storage_slots={"stage": {"target": "/evalhive-stage", "read_only": True}},
        mounts=[
            {"file_id": "transfer-tools", "target": "/evalhive-transfer", "read_only": True},
            {"file_id": "harness-runtime", "target": "/evalhive-runtime", "read_only": True},
        ],
    )
    for key in ("command", "entrypoint", "workdir", "user"):
        if key in description:
            result[key] = deepcopy(description[key])
    result["environment"] = {**result.get("environment", {}), **description.get("environment", {})}
    storage = {}
    protected = [PurePosixPath(value["target"]) for value in result["storage_slots"].values()]
    protected += [PurePosixPath(value["target"]) for value in result["mounts"]]
    protected.append(PurePosixPath("/evalhive-model"))
    for mount in description.get("storage", []):
        target = PurePosixPath(mount["target"])
        if any(target.is_relative_to(other) or other.is_relative_to(target) for other in protected):
            raise HarborConfigurationError(
                "Native Compose storage overlaps an installed control mount"
            )
        protected.append(target)
        slot = "native." + mount["volume"]
        if slot in result["storage_slots"]:
            raise HarborConfigurationError(
                "Native Compose needs separate slots for repeated volume mounts"
            )
        result["storage_slots"][slot] = {
            "target": mount["target"],
            "read_only": mount["read_only"],
            "no_copy": False,
        }
        storage[slot] = mount["volume"]
    if len(result["storage_slots"]) > 16:
        raise HarborConfigurationError("Native Compose storage exceeds installed slot capacity")
    return result, storage


def bind_role(native, selected, role, assets, prefix):
    description = read_compose(native, verifier=role == "verifier")
    images = selected.get("services", {}).get(role, {})
    volumes = selected.get("volumes", {}).get(role, {})
    if description is None and (images or volumes):
        raise HarborConfigurationError("Service bindings have no native Compose source")
    main, cwd = native_asset(
        native, selected[role], verifier=role == "verifier", compose=description
    )
    asset_id = prefix + "." + role
    binding = {"asset_id": asset_id, "cwd": cwd, "user": main.get("user") or "0"}
    assets[asset_id] = main
    if description is None:
        return binding
    if not isinstance(images, dict) or set(images) != set(description["services"]) - {"main"}:
        raise HarborConfigurationError("Native service image bindings must cover the exact group")
    if not isinstance(volumes, dict) or set(volumes) != set(description["volumes"]):
        raise HarborConfigurationError("Native volume budgets must cover the exact group")
    binding.update(services={}, volumes={}, order=description["order"])
    for name, disk in volumes.items():
        if type(disk) is not int or not 1 <= disk <= 1024**2:
            raise HarborConfigurationError(
                "Native service volume needs a bounded explicit MiB budget"
            )
        volume_id = prefix + "." + role + ".volume." + name
        assets[volume_id] = {"kind": "volume", "disk_mb": disk}
        binding["volumes"][name] = volume_id
    for name in description["order"]:
        service = description["services"][name]
        image = selected[role] if name == "main" else images[name]
        result, storage = compose_asset(image, service, original=main if name == "main" else None)
        identity = asset_id if name == "main" else prefix + "." + role + ".service." + name
        assets[identity] = result
        service_binding = {
            "asset_id": identity,
            "cwd": result["workdir"],
            "user": result.get("user") or "0",
            "storage": storage,
            "depends_on": service.get("depends_on", []),
        }
        if name == "main":
            config = (
                resolve_verifier_environment_definition(
                    native.task.config, native.task.paths
                ).config
                if role == "verifier"
                else native.task.config.environment
            )
            service_binding["cwd"] = config.workdir or result["workdir"]
            binding.update(service_binding)
        else:
            binding["services"][name] = service_binding
    return binding


def declarations(installed, worker_image, image_bindings):
    receipt = installed.receipt
    if set(image_bindings) != set(installed.cases):
        raise HarborConfigurationError("Native image bindings must cover the exact installed cases")
    assets = {
        "harbor.worker": container(
            worker_image,
            "worker",
            384,
            0.1,
            384,
            user="0",
            mounts=[{"file_id": "worker-data", "target": "/dataset", "read_only": True}],
        ),
        "harbor.stage": {"kind": "volume", "disk_mb": 64},
        "harbor.verifier-logs": {"kind": "volume", "disk_mb": 64},
        "harbor.agent-artifacts": {"kind": "volume", "disk_mb": 64},
        "harbor.agent-logs": {"kind": "volume", "disk_mb": 64},
        "harbor.public-network": {"kind": "network", "egress": True},
    }
    slots = {
        "stage": {"target": "/evalhive-stage", "read_only": False},
        "verifier-logs": {"target": "/logs/verifier", "read_only": False},
        "agent-artifacts": {"target": "/logs/artifacts", "read_only": False},
        "agent-logs": {"target": "/logs/agent", "read_only": False},
    }
    for mode in ("shared", "separate"):
        assets["harbor.world." + mode] = container(
            worker_image,
            "service",
            128,
            0.1,
            512,
            user="0",
            entrypoint=[],
            command=["/opt/venv/bin/python", "-c", "import time;time.sleep(86400)"],
            storage_slots=deepcopy(slots),
            mounts=[
                {"file_id": "harness-runtime", "target": "/evalhive-runtime", "read_only": True}
            ],
            outputs={
                "native-report": {
                    "path": "/reports/native-verifier",
                    "visibility": "evaluator",
                    "required": False,
                    "stage": "evaluation",
                    "max_archive_bytes": 64 * 1024**2,
                },
                "native-artifacts": {
                    "path": "/outputs/artifacts",
                    "visibility": "user",
                    "required": False,
                    "stage": "capture" if mode == "separate" else "evaluation",
                    "max_archive_bytes": 64 * 1024**2,
                },
                "checkpoint": {
                    "path": "/checkpoint",
                    "visibility": "private",
                    "required": False,
                    "max_archive_bytes": 64 * 1024**2,
                },
            },
        )
    runtime = {"format_version": "1.0", "source_digest": receipt["source_digest"], "cases": {}}
    for case in receipt["cases"]:
        name = case["ref"]["case_id"]
        native = installed.resolve(case)
        selected = image_bindings[name]
        roles = {"task", "verifier"} if native.verifier_mode == "separate" else {"task"}
        if set(selected) - {"services", "volumes"} != roles:
            raise HarborConfigurationError("Native image roles differ from verifier mode")
        for key in ("services", "volumes"):
            if not isinstance(selected.get(key, {}), dict) or set(selected.get(key, {})) - roles:
                raise HarborConfigurationError(
                    "Native service bindings have invalid environment roles"
                )
        binding = {"source_digest": native.source_digest}
        for role in sorted(roles):
            prefix = "harbor.case." + hashlib.sha256(name.encode()).hexdigest()
            binding[role] = bind_role(native, selected, role, assets, prefix)
        runtime["cases"][name] = binding
    resources = {
        "format_version": "1.0",
        "connector_id": CONNECTOR_ID,
        "connector_version": CONNECTOR_VERSION,
        "provider_id": "evalhive.rootless",
        "worker_asset": "harbor.worker",
        "assets": assets,
    }
    return resources, runtime


def install(
    source,
    output,
    *,
    revision,
    worker_image,
    image_bindings,
    transfer_tools,
    dataset_id="terminal-bench-2",
    task_paths=None,
):
    """No Docker execution, provider credential lookup or runtime registration import."""
    source, output, transfer_tools = (
        Path(source).absolute(),
        Path(output).absolute(),
        Path(transfer_tools).absolute(),
    )
    image_pin(worker_image)
    if output.exists() or output.is_symlink():
        raise HarborConfigurationError("Native installation output already exists")
    original = build_receipt(
        source, revision, worker_image, dataset_id=dataset_id, task_paths=task_paths
    )
    installed = InstalledHarbor(source, original)
    resources, runtime = declarations(installed, worker_image, image_bindings)
    names = {path.name for path in transfer_tools.iterdir()}
    if names != {"busybox", "ld-musl-x86_64.so.1"}:
        raise HarborConfigurationError("Native transfer toolchain is unavailable")
    if any(path.is_symlink() or not path.is_file() for path in transfer_tools.iterdir()):
        raise HarborConfigurationError("Native transfer tools must be regular immutable files")
    proof = {name: digest_file(transfer_tools / name) for name in sorted(names)}
    if proof != TRANSFER_TOOLS:
        raise HarborConfigurationError("Native transfer tools differ from the pinned image")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".evalhive-harbor-", dir=output.parent))
    try:
        data = staging / "worker-data"
        (data / "native").mkdir(parents=True)
        for case in original["cases"]:
            relative = case["native_ref"]["path"]
            shutil.copytree(source / relative, data / "native" / relative, symlinks=True)
        copied = build_receipt(
            data / "native",
            revision,
            worker_image,
            dataset_id=dataset_id,
            task_paths=[case["native_ref"]["path"] for case in original["cases"]],
        )
        if copied != original:
            raise HarborConfigurationError("Native source changed during installation")
        write_json(data / "installation.json", copied)
        write_json(data / "runtime.json", runtime)
        shutil.copytree(transfer_tools, staging / "transfer-tools", symlinks=True)
        if proof != {name: digest_file(staging / "transfer-tools" / name) for name in names}:
            raise HarborConfigurationError("Native transfer tools changed during installation")
        write_json(staging / "resources.json", resources)
        tools_digest = (
            "sha256:" + hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()
        )
        write_json(
            staging / "files.json",
            {
                "format_version": "1.0",
                "files": {
                    "worker-data": {
                        "kind": "relative",
                        "path": "worker-data",
                        "source_digest": original["source_digest"],
                    },
                    "transfer-tools": {
                        "kind": "relative",
                        "path": "transfer-tools",
                        "source_digest": tools_digest,
                    },
                    "harness-runtime": {"kind": "runtime_binding", "name": "harness.runtime"},
                },
            },
        )
        (staging / "datasets").mkdir()
        write_json(staging / "datasets" / (dataset_key(dataset_id, revision) + ".json"), original)
        manifest = json.loads(Path(__file__).with_name("manifest.json").read_text())
        manifest["image"] = worker_image
        write_json(staging / "manifest.json", manifest)
        os.rename(staging, output)
        return deepcopy(original)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--worker-image", required=True)
    parser.add_argument(
        "--image-bindings",
        required=True,
        help="Administrator-provided exact image metadata and limits",
    )
    parser.add_argument(
        "--transfer-tools", required=True, help="Export from the pinned worker image"
    )
    parser.add_argument("--dataset-id", default="terminal-bench-2")
    parser.add_argument("--task", action="append", dest="task_paths")
    args = parser.parse_args()
    metadata = Path(args.image_bindings)
    if metadata.stat().st_size > 64 * 1024**2:
        raise HarborConfigurationError("Native image metadata exceeds its byte limit")
    install(
        args.source,
        args.output,
        revision=args.revision,
        worker_image=args.worker_image,
        image_bindings=json.loads(metadata.read_text()),
        transfer_tools=args.transfer_tools,
        dataset_id=args.dataset_id,
        task_paths=args.task_paths,
    )


if __name__ == "__main__":
    main()
