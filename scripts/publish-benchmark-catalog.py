#!/usr/bin/env python3
"""Publish installed benchmark packages as one immutable, backend-free catalog."""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from evalhive_benchmark import validate
from evalhive_benchmark.installation import dataset_key, validate_installation

PIN = re.compile(r"(?:sha256:|[^\s]+@sha256:)[a-f0-9]{64}\Z")


def docker(*arguments):
    result = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("The rootless image inventory is unavailable")
    return result.stdout.strip()


def read_json(path, maximum):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("Package metadata must be bounded installed files")
    return json.loads(path.read_bytes())


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def package_info(source):
    if source.resolve(strict=True) != source or not source.is_dir():
        raise ValueError("Package paths must be absolute installed directories without links")
    for path in source.rglob("*"):
        mode = path.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError("Installed packages cannot contain links or special files")
    manifest = validate("manifest", read_json(source / "manifest.json", 256 * 1024))
    if manifest["kind"] != "benchmark" or not PIN.fullmatch(manifest["image"]):
        raise ValueError("Installed benchmark workers require immutable image pins")
    # A hash-derived directory avoids treating an external identity as a path.
    resources = read_json(source / "resources.json", 4 * 1024**2)
    files = read_json(source / "files.json", 1024**2)
    if (resources.get("connector_id"), resources.get("connector_version")) != (
        manifest["id"],
        manifest["version"],
    ):
        raise ValueError("Resource declarations differ from the installed connector")
    images = {manifest["image"]}
    for asset in resources.get("assets", {}).values():
        if asset.get("kind") == "container":
            image = asset.get("image", "")
            if not isinstance(image, str) or not PIN.fullmatch(image):
                raise ValueError("Native container assets require immutable image pins")
            images.add(image)
    for item in files.get("files", {}).values():
        if item.get("kind") == "relative":
            value = item.get("path", "")
            relative = PurePosixPath(value)
            if (
                not value
                or relative.is_absolute()
                or ".." in relative.parts
                or str(relative) != value
                or "\\" in value
                or not (source / value).exists()
            ):
                raise ValueError("Native file registrations must remain inside the package")
    receipts = {}
    for path in sorted((source / "datasets").glob("*.json")):
        receipt = validate_installation(read_json(path, 64 * 1024**2))
        if (receipt["connector_id"], receipt["connector_version"], receipt["worker_image"]) != (
            manifest["id"],
            manifest["version"],
            manifest["image"],
        ):
            raise ValueError("Dataset receipt differs from its installed worker")
        key = dataset_key(receipt["dataset_id"], receipt["revision"])
        if path.name != key + ".json" or key in receipts:
            raise ValueError("Dataset receipt has an invalid or duplicate identity")
        receipts[key] = receipt
    if not receipts:
        raise ValueError("A runtime package must contain installed dataset receipts")
    return manifest, receipts, images


def publish(sources, destination):
    security = json.loads(docker("info", "--format", "{{json .SecurityOptions}}"))
    if "name=rootless" not in security:
        raise ValueError("Select the EvalHive rootless Docker daemon")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Catalog already exists; select a new release directory")
    packages, groups, images = [], {}, set()
    for source in sources:
        manifest, receipts, pins = package_info(source)
        identity = manifest["id"]
        group = groups.setdefault(identity, {"manifest": manifest, "receipts": {}})
        if group["manifest"] != manifest or group["receipts"].keys() & receipts.keys():
            raise ValueError("A connector requires one manifest and unique dataset revisions")
        group["receipts"].update(receipts)
        images.update(pins)
        packages.append((source, identity, receipts))
    if not packages:
        raise ValueError("Select at least one installed benchmark package")
    if any(len(group["receipts"]) > 4096 for group in groups.values()):
        raise ValueError("A connector catalog exceeds its installation index limit")
    inventory = {}
    for image in sorted(images):
        resolved = docker("image", "inspect", image, "--format", "{{.Id}}")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", resolved):
            raise ValueError("An installed native image has no immutable local identity")
        inventory[image] = resolved
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".benchmark-catalog-", dir=destination.parent))
    try:
        for identity, group in groups.items():
            name = dataset_key(identity, group["manifest"]["version"])
            root = staging / name
            write_json(root / "manifest.json", group["manifest"])
            index = {}
            for source, connector, receipts in packages:
                if connector != identity:
                    continue
                # One copied native package can advertise more than one receipt.
                relative = "revisions/" + sorted(receipts)[0]
                shutil.copytree(source, root / relative)
                for key, receipt in receipts.items():
                    index[key] = relative
                    write_json(root / "datasets" / (key + ".json"), receipt)
            write_json(
                root / "installations.json", {"format_version": "1.0", "installations": index}
            )
        write_json(staging / "images.json", {"format_version": "1.0", "images": inventory})
        # Keep each installed image addressable when a development tag is moved.
        for image, resolved in inventory.items():
            docker("image", "tag", image, "evalhive-benchmark-retained:" + resolved[7:])
        os.rename(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {
        "connectors": len(groups),
        "datasets": sum(len(g["receipts"]) for g in groups.values()),
        "images": len(inventory),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", action="append", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = publish([path.absolute() for path in args.package], args.destination.absolute())
    except Exception:
        # Native filesystem names and JSON validation errors can contain hidden data.
        raise SystemExit(
            "Catalog publication failed; verify installed packages, image pins and destination"
        ) from None
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
