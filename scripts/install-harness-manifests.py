#!/usr/bin/env python3
"""Install public connector declarations pinned to local immutable image IDs."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def docker(*arguments):
    result = subprocess.run(["docker", *arguments], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def image_id(reference):
    return docker("image", "inspect", reference, "--format", "{{.Id}}")


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def install(source, destination):
    security = docker("info", "--format", "{{json .SecurityOptions}}")
    if security is None or "name=rootless" not in json.loads(security):
        raise ValueError("Select the EvalHive rootless Docker daemon")
    inventory, declarations = {}, {}
    for path in sorted(source.glob("*/manifest.json")):
        manifest = json.loads(path.read_bytes())
        identity = manifest["id"]
        if identity in inventory:
            raise ValueError("Duplicate installed connector identity")
        original = manifest["image"]
        resolved = image_id(original)
        if not resolved:
            raise ValueError("A required connector image is not installed")
        manifest["image"] = resolved
        runtime_bundle = manifest.get("requirements", {}).get("runtime_bundle")
        bundle_inventory = None
        if runtime_bundle is not None:
            bundle_reference = runtime_bundle["image"]
            bundle_id = image_id(bundle_reference)
            if not bundle_id:
                raise ValueError("A required connector runtime bundle is not installed")
            runtime_bundle["image"] = bundle_id
            bundle_inventory = {"reference": bundle_reference, "image_id": bundle_id}
        environments = {}
        variants = manifest.get("requirements", {}).get("environment_images", {})
        for name, reference in variants.items():
            pinned = image_id(reference)
            environments[name] = {"reference": reference, "image_id": pinned, "installed": pinned is not None}
            if pinned:
                variants[name] = pinned
        declarations[path.parent.name] = encoded(manifest)
        inventory[identity] = {"connector_version": manifest["version"], "reference": original,
                               "image_id": resolved, "environments": environments, "runtime_bundle": bundle_inventory,
                               "installed_manifest_sha256": hashlib.sha256(declarations[path.parent.name]).hexdigest()}
    if not declarations:
        raise ValueError("No connector declarations found")
    # Never replace an existing release's catalog or make partial declarations
    # visible to a running backend. Publish a new directory atomically.
    staging = destination.with_name(destination.name + f".staging-{os.getpid()}")
    if destination.exists() or staging.exists():
        raise ValueError("Catalog destination already exists; choose a new release directory")
    staging.mkdir(parents=True)
    try:
        for name, content in declarations.items():
            folder = staging / name
            folder.mkdir()
            (folder / "manifest.json").write_bytes(content)
        (staging / "images.json").write_bytes(encoded(inventory))
        staging.rename(destination)
    except BaseException:
        import shutil

        shutil.rmtree(staging)
        raise
    return len(inventory)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("harnesses"))
    parser.add_argument("--destination", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        count = install(arguments.source, arguments.destination)
    except (ValueError, OSError, KeyError):
        raise SystemExit("Harness catalog installation failed; verify declarations, images and destination") from None
    print(f"Installed {count} connector declarations with immutable local image IDs")
