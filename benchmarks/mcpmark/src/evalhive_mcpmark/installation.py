"""Install frozen initial state and declarative, independently owned native resources."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from evalhive_benchmark.installation import dataset_key

from .assets import filesystem_asset, postgres_asset
from .dataset import CONNECTOR_ID, CONNECTOR_VERSION, build_receipt
from .native import NativeConfigurationError, fingerprint, inventory


def image_pin(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:sha256:|[^\s]+@sha256:)[a-f0-9]{64}", value
    ):
        raise NativeConfigurationError("Native registration requires immutable image identities")
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


def declarations(receipt, worker_image, task_image, *, postgres_image=None):
    common = [{"file_id": "harness-runtime", "target": "/evalhive-runtime", "read_only": True}]
    assets = {
        "mcpmark.worker": container(
            worker_image,
            "worker",
            256,
            0.25,
            256,
            user="0",
            mounts=[{"file_id": "worker-data", "target": "/dataset", "read_only": True}],
        ),
        "mcpmark.working": {"kind": "volume", "disk_mb": 256},
        "mcpmark.connection": {"kind": "volume", "disk_mb": 1},
        "mcpmark.world": container(
            worker_image,
            "service",
            512,
            0.5,
            512,
            user="0",
            command=["/opt/venv/bin/python", "-m", "evalhive_mcpmark.world_service"],
            mounts=[
                *common,
                {"file_id": "worker-data", "target": "/dataset", "read_only": True},
                {"file_id": "initial-state", "target": "/initial", "read_only": True},
            ],
            storage_slots={
                "working": {"target": "/opt/native/.mcpmark_backups", "read_only": False},
                "connection": {"target": "/connection", "read_only": False},
            },
            outputs={
                name: {
                    "path": "/records/" + name,
                    "visibility": "evaluator",
                    "required": False,
                    "stage": "evaluation" if name == "report" else "capture",
                    "max_archive_bytes": 10 * 1024**2,
                }
                for name in ("transcript", "report")
            },
        ),
        "mcpmark.task": container(
            task_image,
            "environment",
            1024,
            1,
            512,
            user="0",
            workdir="/workspace",
            mounts=common,
            storage_slots={
                "working": {"target": "/opt/native/.mcpmark_backups", "read_only": False},
                "connection": {"target": "/evalhive-connection", "read_only": True},
            },
            outputs={
                "working": {
                    "path": "/opt/native/.mcpmark_backups",
                    "visibility": "user",
                    "required": False,
                    "stage": "capture",
                    "max_archive_bytes": 64 * 1024**2,
                }
            },
        ),
    }
    service = receipt["provenance"]["service"]
    names = ["working", "connection", "world", "task"]
    if service == "postgres":
        image_pin(postgres_image)
        names += ["network", "data", "secret", "postgres"]
        assets.update(
            {
                "mcpmark.network": {"kind": "network", "egress": False},
                "mcpmark.data": {"kind": "volume", "disk_mb": 1024},
                "mcpmark.secret": {"kind": "volume", "disk_mb": 1},
                "mcpmark.postgres": container(
                    postgres_image,
                    "service",
                    512,
                    0.25,
                    256,
                    user="999:999",
                    command=[
                        "sh",
                        "-c",
                        "count=0; while [ ! -s /postgres-secret/password ]; do count=$((count+1)); [ $count -le 600 ] || exit 1; sleep 0.2; done; exec docker-entrypoint.sh postgres -c shared_buffers=32MB -c max_connections=20",
                    ],
                    environment={
                        "POSTGRES_USER": "postgres",
                        "POSTGRES_DB": "postgres",
                        "POSTGRES_PASSWORD_FILE": "/postgres-secret/password",
                    },
                    mounts=[
                        *common,
                        {
                            "file_id": "initial-state",
                            "target": "/opt/native/postgres_state",
                            "read_only": True,
                        },
                    ],
                    storage_slots={
                        "data": {
                            "target": "/var/lib/postgresql/data",
                            "read_only": False,
                            "no_copy": False,
                        },
                        "secret": {"target": "/postgres-secret", "read_only": True},
                    },
                ),
            }
        )
        world, task = assets["mcpmark.world"], assets["mcpmark.task"]
        for mount in world["mounts"]:
            if mount["file_id"] == "initial-state":
                mount["target"] = "/opt/native/postgres_state"
        world["storage_slots"]["secret"] = {"target": "/postgres-secret", "read_only": False}
        for member in (world, task):
            member["storage_slots"]["working"]["target"] = "/workspace"
        task["outputs"]["working"]["path"] = "/workspace"
    elif service != "filesystem":
        raise NativeConfigurationError("Native executable service is not installed")
    return (
        {
            "format_version": "1.0",
            "connector_id": CONNECTOR_ID,
            "connector_version": CONNECTOR_VERSION,
            "provider_id": "evalhive.rootless",
            "worker_asset": "mcpmark.worker",
            "assets": assets,
        },
        {
            "format_version": "1.0",
            "source_digest": receipt["source_digest"],
            "assets": {name: "mcpmark." + name for name in names},
            "working_path": "/workspace"
            if service == "postgres"
            else "/opt/native/.mcpmark_backups",
        },
    )


def install(
    source,
    output,
    *,
    initial_root,
    categories=None,
    revision,
    worker_image,
    task_image,
    suite="standard",
    dataset_id=None,
    service="filesystem",
    postgres_image=None,
):
    """Publish an installation atomically; downloading is an earlier admin operation."""
    source, output, initial_root = map(
        lambda path: Path(path).absolute(), (source, output, initial_root)
    )
    image_pin(worker_image)
    image_pin(task_image)
    if (
        output.exists()
        or output.is_symlink()
        or service not in {"filesystem", "postgres"}
        or service == "filesystem"
        and (not categories or len(categories) != len(set(categories)))
        or service == "postgres"
        and categories
    ):
        raise NativeConfigurationError("Native installation output or categories are invalid")

    def inspect_initial(root):
        return (
            [filesystem_asset(root, category) for category in sorted(categories)]
            if service == "filesystem"
            else [postgres_asset(root)]
        )

    assets = inspect_initial(initial_root)
    receipt = build_receipt(
        source,
        revision,
        worker_image,
        service=service,
        suite=suite,
        dataset_id=dataset_id,
        assets=assets,
    )
    resources, runtime = declarations(
        receipt, worker_image, task_image, postgres_image=postgres_image
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".evalhive-mcpmark-", dir=output.parent))
    try:
        data = staging / "worker-data"
        data.mkdir(mode=0o700)
        for record in receipt["provenance"]["files"]:
            target = data / "native" / record["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            with (source / record["path"]).open("rb") as origin, target.open("xb") as destination:
                shutil.copyfileobj(origin, destination)
        initial = staging / "initial-state"
        initial.mkdir(mode=0o700)
        if service == "filesystem":
            for category in sorted(categories):
                shutil.copytree(
                    initial_root / category, initial / category, copy_function=shutil.copy2
                )
        else:
            for record in assets[0]["files"]:
                shutil.copy2(initial_root / record["path"], initial / record["path"])
        if (
            inventory(data / "native", service, suite) != receipt["provenance"]["files"]
            or inventory(source, service, suite) != receipt["provenance"]["files"]
            or inspect_initial(initial) != assets
            or inspect_initial(initial_root) != assets
        ):
            raise NativeConfigurationError("Native installation inputs changed while copying")
        write_json(data / "installation.json", receipt)
        write_json(data / "runtime.json", runtime)
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
                    "initial-state": {
                        "kind": "relative",
                        "path": "initial-state",
                        "source_digest": fingerprint(assets),
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
    for name in ("source", "output", "initial-root", "revision", "worker-image", "task-image"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--categories", nargs="+")
    parser.add_argument("--service", default="filesystem", choices=["filesystem", "postgres"])
    parser.add_argument("--postgres-image")
    parser.add_argument("--suite", default="standard")
    parser.add_argument("--dataset-id")
    args = vars(parser.parse_args())
    source, output = args.pop("source"), args.pop("output")
    install(source, output, **args)


if __name__ == "__main__":
    main()
