"""Install complete catalogs and explicitly inspected, immutable instance images."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from pathlib import Path

from evalhive_benchmark.installation import dataset_key

from .dataset import CONNECTOR_ID, CONNECTOR_VERSION, build_receipt
from .native import NativeConfigurationError, canonical, inventory


def image_pin(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:sha256:|[^\s]+@sha256:)[a-f0-9]{64}", value
    ):
        raise NativeConfigurationError("Native registration requires immutable image identities")
    return value


def container(image, role, memory, cpu, disk, **settings):
    return {
        "kind": "container",
        "role": role,
        "image": image_pin(image),
        "limits": {"memory_mb": memory, "cpu_cores": cpu, "pids": 256},
        "minimum": {"memory_mb": memory, "cpu_cores": cpu, "disk_mb": disk},
        "user": "0",
        **settings,
    }


def limits(selected):
    if not isinstance(selected, dict) or set(selected) != {"memory_mb", "cpu_cores", "disk_mb"}:
        raise NativeConfigurationError("Native instance limits must be explicit")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        for value in selected.values()
    ):
        raise NativeConfigurationError("Native instance limits must be finite and positive")
    if type(selected["memory_mb"]) is not int or type(selected["disk_mb"]) is not int:
        raise NativeConfigurationError("Native memory and disk limits must be whole MiB")
    return selected["memory_mb"], selected["cpu_cores"], selected["disk_mb"]


def declarations(receipt, worker_image, image_bindings):
    runtime = {"file_id": "harness-runtime", "target": "/evalhive-runtime", "read_only": True}
    private = {"file_id": "worker-data", "target": "/dataset", "read_only": True}
    catalog = {"file_id": "catalog-data", "target": "/dataset", "read_only": True}
    tools = {"file_id": "transfer-tools", "target": "/evalhive-tools", "read_only": True}
    assets = {
        "swebench.worker": container(worker_image, "worker", 256, 0.25, 256, mounts=[catalog]),
        "swebench.capture": {"kind": "volume", "disk_mb": 16},
        "swebench.stage": {"kind": "volume", "disk_mb": 16},
        "swebench.logs": {"kind": "volume", "disk_mb": 128},
        "swebench.world": container(
            worker_image,
            "service",
            512,
            0.25,
            256,
            command=["/opt/venv/bin/python", "-m", "evalhive_swebench.world_service"],
            mounts=[runtime, private],
            storage_slots={
                "capture": {"target": "/evalhive-capture", "read_only": True},
                "stage": {"target": "/stage", "read_only": False},
                "logs": {"target": "/evalhive-results", "read_only": True},
            },
            outputs={
                "patch": {
                    "path": "/checkpoint",
                    "visibility": "user",
                    "required": False,
                    "stage": "capture",
                    "max_archive_bytes": 16 * 1024**2,
                },
                "report": {
                    "path": "/records",
                    "visibility": "evaluator",
                    "required": False,
                    "stage": "evaluation",
                    "max_archive_bytes": 128 * 1024**2,
                },
            },
        ),
    }
    cases = {}
    expected = receipt["provenance"]["installed_images"]
    if set(image_bindings) != set(expected):
        raise NativeConfigurationError("Native runtime images differ from the catalog receipt")
    for case in receipt["cases"]:
        identity = case["ref"]["case_id"]
        if identity not in image_bindings:
            cases[identity] = {"available": False, "reason": "NATIVE_IMAGE_NOT_INSTALLED"}
            continue
        selected = image_bindings[identity]
        if (
            set(selected)
            != {
                "image",
                "base_commit",
                "image_commit",
                "reserved_paths_checked",
                "task_limits",
                "evaluator_limits",
            }
            or selected["reserved_paths_checked"] is not True
            or selected["image"] != expected[identity]
            or selected["base_commit"] != case["native_ref"]["base_commit"]
            or not isinstance(selected["image_commit"], str)
            or not re.fullmatch(r"[a-f0-9]{40}", selected["image_commit"])
        ):
            raise NativeConfigurationError("Native image metadata must be explicitly inspected")
        prefix = "swebench.case." + hashlib.sha256(identity.encode()).hexdigest()
        cases[identity] = {
            "available": True,
            "source_digest": case["source_digest"],
            "image_commit": selected["image_commit"],
        }
        for role in ("task", "evaluator"):
            asset = prefix + "." + role
            settings = {
                "command": ["sleep", "infinity"],
                "workdir": "/testbed",
                "mounts": [runtime, tools],
                "environment": {
                    "PATH": "/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/bin:/bin"
                },
                "storage_slots": {"capture": {"target": "/evalhive-capture", "read_only": False}}
                if role == "task"
                else {
                    "stage": {"target": "/stage", "read_only": True},
                    "logs": {"target": "/evalhive-results", "read_only": False},
                },
            }
            if role == "task":
                settings["agent_process_policy"] = "retain_until_cleanup"
            assets[asset] = container(
                selected["image"],
                "environment" if role == "task" else "grader",
                *limits(selected[role + "_limits"]),
                **settings,
            )
            cases[identity][role] = asset
    return {
        "format_version": "1.0",
        "connector_id": CONNECTOR_ID,
        "connector_version": CONNECTOR_VERSION,
        "provider_id": "evalhive.rootless",
        "worker_asset": "swebench.worker",
        "assets": assets,
    }, {"format_version": "1.0", "source_digest": receipt["source_digest"], "cases": cases}


def write_json(path, value):
    with Path(path).open("xb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def install(
    source,
    output,
    *,
    revision,
    worker_image,
    image_bindings,
    subset="verified",
    split="test",
    dataset_id=None,
):
    source, output = Path(source).absolute(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise NativeConfigurationError("Native installation output already exists")
    receipt = build_receipt(
        source,
        revision,
        image_pin(worker_image),
        subset=subset,
        split=split,
        dataset_id=dataset_id,
        installed_images={key: image_pin(value["image"]) for key, value in image_bindings.items()},
    )
    resources, bindings = declarations(receipt, worker_image, image_bindings)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".evalhive-swebench-", dir=output.parent))
    try:
        data = staging / "worker-data"
        data.mkdir(mode=0o700)
        for record in receipt["provenance"]["files"]:
            target = data / "native" / record["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            with (source / record["path"]).open("rb") as origin, target.open("xb") as destination:
                shutil.copyfileobj(origin, destination)
        identities = [case["ref"]["case_id"] for case in receipt["cases"]]
        if any(
            inventory(root, identities) != receipt["provenance"]["files"]
            for root in (source, data / "native")
        ):
            raise NativeConfigurationError("Native task repository changed during installation")
        tools = staging / "transfer-tools"
        tools.mkdir()
        for name in ("task_transfer.py", "eval_transfer.py"):
            shutil.copyfile(Path(__file__).with_name(name), tools / name)
        write_json(data / "installation.json", receipt)
        write_json(data / "runtime.json", bindings)
        catalog = staging / "catalog-data"
        catalog.mkdir(mode=0o700)
        write_json(catalog / "installation.json", receipt)
        write_json(catalog / "runtime.json", bindings)
        write_json(staging / "resources.json", resources)
        write_json(
            staging / "files.json",
            {
                "format_version": "1.0",
                "files": {
                    "catalog-data": {
                        "kind": "relative",
                        "path": "catalog-data",
                        "source_digest": receipt["source_digest"],
                    },
                    "worker-data": {
                        "kind": "relative",
                        "path": "worker-data",
                        "source_digest": receipt["source_digest"],
                    },
                    "transfer-tools": {
                        "kind": "relative",
                        "path": "transfer-tools",
                        "source_digest": "sha256:"
                        + hashlib.sha256(
                            canonical(
                                {
                                    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in tools.iterdir()
                                }
                            )
                        ).hexdigest(),
                    },
                    "harness-runtime": {"kind": "runtime_binding", "name": "harness.runtime"},
                },
            },
        )
        (staging / "datasets").mkdir()
        write_json(
            staging / "datasets" / (dataset_key(receipt["dataset_id"], revision) + ".json"), receipt
        )
        manifest = json.loads(Path(__file__).with_name("manifest.json").read_text())
        manifest["image"] = worker_image
        write_json(staging / "manifest.json", manifest)
        os.rename(staging, output)
        return receipt
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("output")
    for name in ("revision", "worker-image", "image-bindings"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--subset", default="verified", choices=("verified", "lite", "full"))
    parser.add_argument("--split", default="test", choices=("dev", "test"))
    parser.add_argument("--dataset-id")
    args = vars(parser.parse_args())
    args["image_bindings"] = json.loads(Path(args["image_bindings"]).read_text())
    install(**args)


if __name__ == "__main__":
    main()
