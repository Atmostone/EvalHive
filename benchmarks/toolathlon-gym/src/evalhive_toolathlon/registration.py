"""Publish declarative runtime registration next to an immutable native installation."""

import argparse
import json
import os
import re
from pathlib import Path

from evalhive_benchmark.installation import dataset_key
from evalhive_cases.install import write_json

from .commands import NativeConfigurationError
from .dataset import InstalledToolathlon
from .installation import assets


def image_pin(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:sha256:|[^\s]+@sha256:)[a-f0-9]{64}", value
    ):
        raise NativeConfigurationError("Runtime registration requires immutable image pins")
    return value


def register(root, *, environment_image, postgres_image, egress=True):
    root = Path(root)
    installed = InstalledToolathlon(root)
    receipt = installed.receipt
    worker_image = image_pin(receipt["worker_image"])
    environment_image, postgres_image = image_pin(environment_image), image_pin(postgres_image)
    manifest = json.loads(Path(__file__).with_name("manifest.json").read_text())
    manifest["image"] = worker_image
    if any(
        (root / name).exists()
        for name in ("manifest.json", "resources.json", "files.json", "datasets")
    ):
        raise NativeConfigurationError("Runtime registration already exists")
    document = assets(worker_image, environment_image, postgres_image, egress=egress)
    files = {
        "format_version": "1.0",
        "files": {
            "cases": {
                "kind": "relative",
                "path": "cases",
                "source_digest": receipt["source_digest"],
            },
            "native-source": {
                "kind": "relative",
                "path": "native-source.tar",
                "source_digest": installed.archive.digest,
            },
            "harness-runtime": {"kind": "runtime_binding", "name": "harness.runtime"},
        },
    }
    # Manifest is the publication marker and is written only after its data files.
    (root / "datasets").mkdir()
    write_json(
        root / "datasets" / (dataset_key(receipt["dataset_id"], receipt["revision"]) + ".json"),
        receipt,
    )
    write_json(root / "resources.json", document)
    write_json(root / "files.json", files)
    pending = root / ".manifest.pending.json"
    write_json(pending, manifest)
    os.link(pending, root / "manifest.json")
    pending.unlink()
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--environment-image", required=True)
    parser.add_argument("--postgres-image", required=True)
    parser.add_argument("--offline-network", action="store_true")
    args = parser.parse_args()
    register(
        args.root,
        environment_image=args.environment_image,
        postgres_image=args.postgres_image,
        egress=not args.offline_network,
    )


if __name__ == "__main__":
    main()
