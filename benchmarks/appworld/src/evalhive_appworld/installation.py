"""Publish private native data and declarative runtime assets atomically."""

from __future__ import annotations

import argparse
import json
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
        **settings,
    }


def declarations(receipt, worker_image, task_image, *, world_image=None):
    runtime = [{"file_id": "harness-runtime", "target": "/evalhive-runtime", "read_only": True}]
    private = [{"file_id": "worker-data", "target": "/dataset", "read_only": True}]
    outputs = {
        name: {
            "path": path,
            "visibility": "evaluator",
            "required": False,
            "stage": stage,
            "max_archive_bytes": 64 * 1024**2,
        }
        for name, path, stage in (
            ("state", "/appworld/experiments/outputs", "capture"),
            ("records", "/records", "capture"),
            ("report", "/records/report", "evaluation"),
        )
    }
    assets = {
        "appworld.worker": container(
            worker_image,
            "worker",
            512,
            0.25,
            256,
            user="0",
            mounts=private,
            environment={
                "APPWORLD_ROOT": "/dataset/native",
                "APPWORLD_CACHE": "/tmp/appworld-worker-cache",
            },
        ),
        "appworld.working": {"kind": "volume", "disk_mb": 128},
        "appworld.connection": {"kind": "volume", "disk_mb": 1},
        "appworld.state": {"kind": "volume", "disk_mb": 256},
        "appworld.network": {"kind": "network", "egress": False},
        "appworld.world": container(
            world_image or worker_image,
            "service",
            1024,
            0.75,
            512,
            user="0",
            command=["/opt/venv/bin/python", "-m", "evalhive_appworld.world_service"],
            environment={
                "APPWORLD_ROOT": "/appworld",
                "APPWORLD_CACHE": "/tmp/appworld-world-cache",
            },
            mounts=[
                *runtime,
                *private,
                {"file_id": "native-data", "target": "/appworld/data", "read_only": True},
            ],
            storage_slots={
                "connection": {"target": "/connection", "read_only": False},
                "state": {"target": "/appworld/experiments/outputs", "read_only": False},
            },
            outputs=outputs,
            finalizer={
                "argv": ["/opt/venv/bin/python", "-m", "evalhive_benchmark.finalizer"],
                "timeout_seconds": 120,
                "replay_safe": True,
            },
        ),
        "appworld.task": container(
            task_image,
            "environment",
            1280,
            1,
            384,
            user="0",
            workdir="/workspace",
            mounts=runtime,
            storage_slots={
                "connection": {"target": "/evalhive-connection", "read_only": True},
                "working": {"target": "/workspace", "read_only": False},
            },
            outputs={
                "working": {
                    "path": "/workspace",
                    "visibility": "user",
                    "required": False,
                    "stage": "capture",
                    "max_archive_bytes": 64 * 1024**2,
                }
            },
        ),
    }
    return (
        {
            "format_version": "1.0",
            "connector_id": CONNECTOR_ID,
            "connector_version": CONNECTOR_VERSION,
            "provider_id": "evalhive.rootless",
            "worker_asset": "appworld.worker",
            "assets": assets,
        },
        {
            "format_version": "1.0",
            "source_digest": receipt["source_digest"],
            "assets": {
                name: "appworld." + name
                for name in ("working", "connection", "state", "network", "world", "task")
            },
        },
    )


def write_json(path, value):
    with Path(path).open("xb") as stream:
        stream.write(canonical(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def install(
    source, output, *, split, revision, worker_image, task_image, world_image=None, dataset_id=None
):
    source, output = Path(source).absolute(), Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise NativeConfigurationError("Native installation output already exists")
    receipt = build_receipt(
        source, revision, image_pin(worker_image), split=split, dataset_id=dataset_id
    )
    resources, bindings = declarations(receipt, worker_image, task_image, world_image=world_image)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".evalhive-appworld-", dir=output.parent))
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
            inventory(root, split, identities) != receipt["provenance"]["files"]
            for root in (source, data / "native")
        ):
            raise NativeConfigurationError("Native dataset changed during installation")
        write_json(data / "installation.json", receipt)
        write_json(data / "runtime.json", bindings)
        write_json(staging / "resources.json", resources)
        write_json(
            staging / "files.json",
            {
                "format_version": "1.0",
                "files": {
                    "worker-data": {
                        "kind": "relative",
                        "path": "worker-data",
                        "source_digest": receipt["source_digest"],
                    },
                    "native-data": {
                        "kind": "relative",
                        "path": "worker-data/native/data",
                        "source_digest": receipt["source_digest"],
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
    for name in ("split", "revision", "worker-image", "task-image"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--world-image")
    parser.add_argument("--dataset-id")
    install(**vars(parser.parse_args()))


if __name__ == "__main__":
    main()
