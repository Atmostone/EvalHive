"""Snapshot private native data and register immutable worker and Task assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import evalhive_benchmark
from evalhive_benchmark.installation import dataset_key

from .dataset import CONNECTOR_ID, CONNECTOR_VERSION, build_receipt
from .native import NativeConfigurationError, fingerprint, inventory
from .resources import public_surface
from .stage import MAX_BYTES


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


def declarations(receipt, surface, worker_image, task_image):
    outputs = {
        name: {
            "path": "/records/" + name,
            "visibility": "private" if name == "checkpoint" else "evaluator",
            "required": False,
            "stage": "evaluation" if name == "report" else "capture",
            "max_archive_bytes": MAX_BYTES + 1024**2,
        }
        for name in ("trace", "checkpoint", "report")
    }
    assets = {
        "tau2.worker": container(
            worker_image,
            "worker",
            768,
            0.5,
            512,
            user="0",
            mounts=[{"file_id": "worker-data", "target": "/dataset", "read_only": True}],
        ),
        "tau2.recorder": container(
            worker_image,
            "service",
            128,
            0.1,
            64,
            user="0",
            command=["/opt/venv/bin/python", "-c", "import time; time.sleep(86400)"],
            mounts=[
                {"file_id": "harness-runtime", "target": "/evalhive-runtime", "read_only": True}
            ],
            outputs=outputs,
        ),
        "tau2.task": container(
            task_image,
            "environment",
            1024,
            0.5,
            512,
            user="1000:1000",
            workdir="/workspace",
            command=["/opt/environment/venv/bin/python", "-c", "import time; time.sleep(86400)"],
            mounts=[
                {"file_id": identity, "target": target, "read_only": True}
                for identity, target in (
                    ("public-surface", "/evalhive-public"),
                    ("public-sdk", "/opt/public-sdk"),
                    ("harness-runtime", "/evalhive-runtime"),
                )
            ],
        ),
    }
    return (
        {
            "format_version": "1.0",
            "connector_id": CONNECTOR_ID,
            "connector_version": CONNECTOR_VERSION,
            "provider_id": "evalhive.rootless",
            "worker_asset": "tau2.worker",
            "assets": assets,
        },
        {
            "format_version": "1.0",
            "source_digest": receipt["source_digest"],
            "surface_fingerprint": surface["fingerprint"],
            "assets": {"task": "tau2.task", "recorder": "tau2.recorder"},
        },
    )


def public_sdk(source, destination):
    result = []
    for path in sorted(source.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise NativeConfigurationError("Public SDK source must not contain links")
        if path.is_dir():
            continue
        if not path.is_file() or path.stat().st_size > 2 * 1024**2:
            raise NativeConfigurationError("Public SDK source exceeds its regular-file budget")
        data = path.read_bytes()
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
        target.chmod(0o444)
        result.append(
            {
                "path": str(path.relative_to(source)),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
        )
    return fingerprint(result)


def install(
    source, output, *, domain, revision, worker_image, task_image, split="base", dataset_id=None
):
    """Runs only in an administrator's isolated native catalog environment."""
    source, output = Path(source).absolute(), Path(output).absolute()
    image_pin(worker_image)
    image_pin(task_image)
    if output.exists() or output.is_symlink():
        raise NativeConfigurationError("Native installation output already exists")
    receipt = build_receipt(
        source, revision, worker_image, domain=domain, split=split, dataset_id=dataset_id
    )
    surface = public_surface(source, domain)
    resources, runtime = declarations(receipt, surface, worker_image, task_image)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".evalhive-tau2-", dir=output.parent))
    try:
        data = staging / "worker-data"
        data.mkdir(mode=0o700)
        for record in receipt["provenance"]["files"]:
            relative = record["path"]
            target = data / "native" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with (source / relative).open("rb") as origin, target.open("xb") as destination:
                shutil.copyfileobj(origin, destination)
        if (
            inventory(data / "native", domain) != receipt["provenance"]["files"]
            or inventory(source, domain) != receipt["provenance"]["files"]
        ):
            raise NativeConfigurationError("Native source changed during installation")
        write_json(data / "installation.json", receipt)
        write_json(data / "runtime.json", runtime)
        public = staging / "public-surface"
        public.mkdir()
        write_json(public / "tools.json", surface["catalog"])
        (public / "instructions.md").write_text(surface["instructions"])
        sdk = staging / "public-sdk"
        sdk.mkdir()
        sdk_digest = public_sdk(
            Path(evalhive_benchmark.__file__).parent, sdk / "evalhive_benchmark"
        )
        for directory in [staging, public, sdk, *[p for p in sdk.rglob("*") if p.is_dir()]]:
            directory.chmod(0o755)
        for path in public.iterdir():
            path.chmod(0o444)
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
                    "public-surface": {
                        "kind": "relative",
                        "path": "public-surface",
                        "source_digest": "sha256:" + surface["fingerprint"],
                    },
                    "public-sdk": {
                        "kind": "relative",
                        "path": "public-sdk",
                        "source_digest": sdk_digest,
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
    for name in ("source", "output", "domain", "revision", "worker-image", "task-image"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--split", default="base")
    parser.add_argument("--dataset-id")
    args = vars(parser.parse_args())
    source, output = args.pop("source"), args.pop("output")
    install(source, output, **args)


if __name__ == "__main__":
    main()
