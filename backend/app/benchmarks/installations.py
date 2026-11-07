"""Resolve administrator-installed dataset packages without importing native code."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from evalhive_benchmark import validate
from evalhive_benchmark.installation import dataset_key, installation_digest, validate_installation
from evalhive_benchmark.peer import identity
from jsonschema import Draft202012Validator

from app.benchmarks.catalog import CatalogError
from app.benchmarks.datasets import installed_dataset
from app.config import get_settings

INDEX_SCHEMA = {
    "type": "object",
    "required": ["format_version", "installations"],
    "additionalProperties": False,
    "properties": {
        "format_version": {"const": "1.0"},
        "installations": {
            "type": "object",
            "maxProperties": 4096,
            "propertyNames": {"pattern": "^[a-f0-9]{64}$"},
            "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 4096},
        },
    },
}


@dataclass(frozen=True)
class InstalledPackage:
    directory: Path
    manifest: dict
    receipt: dict
    metadata_digest: str


def read_json(path, maximum):
    if (
        path.absolute() != path.resolve(strict=True)
        or not path.is_file()
        or path.stat().st_size > maximum
    ):
        raise CatalogError("Runtime package metadata must be bounded installed files")
    with path.open("rb") as stream:
        content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise CatalogError("Runtime package metadata exceeds its byte budget")
    return json.loads(content)


def resolve_package(dataset, *, directory=None):
    """Each revision may bind a distinct native environment under one connector."""
    root = Path(directory or get_settings().benchmark_manifest_dir).absolute()
    try:
        manifest, receipt = installed_dataset(
            dataset.connector_id,
            dataset.connector_version,
            dataset.dataset_id,
            dataset.revision,
            directory=root,
        )
        if (
            manifest != dataset.manifest
            or installation_digest(receipt) != dataset.receipt_digest
            or receipt["worker_image"] != dataset.worker_image
        ):
            raise CatalogError("Installed dataset differs from its registered revision")
        candidates = [
            path.parent
            for path in root.glob("*/manifest.json")
            if read_json(path, 256 * 1024) == manifest
        ]
        if len(candidates) != 1:
            raise CatalogError("A single installed connector package is required")
        catalog = candidates[0]
        key = dataset_key(dataset.dataset_id, dataset.revision)
        index_path = catalog / "installations.json"
        if index_path.exists() or index_path.is_symlink():
            index = read_json(index_path, 1024**2)
            if not Draft202012Validator(INDEX_SCHEMA).is_valid(index):
                raise CatalogError("Installed runtime package index is invalid")
            value = index["installations"].get(key)
            if value is None:
                raise CatalogError("Dataset runtime package is not registered")
            relative = PurePosixPath(value)
            if (
                relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
                or str(relative) != value
                or "\\" in value
            ):
                raise CatalogError("Runtime package requires a contained relative directory")
            package = catalog / str(relative)
        else:
            package = catalog
        if (
            package.resolve(strict=True) != package
            or not package.is_dir()
            or not package.is_relative_to(catalog)
        ):
            raise CatalogError("Runtime package must remain inside its installed connector")
        native_manifest = validate("manifest", read_json(package / "manifest.json", 256 * 1024))
        native_receipt = validate_installation(
            read_json(package / "datasets" / (key + ".json"), 64 * 1024**2)
        )
        if native_manifest != manifest or native_receipt != receipt:
            raise CatalogError("Runtime package differs from its registered manifest or dataset")
        resources = read_json(package / "resources.json", 4 * 1024**2)
        files = read_json(package / "files.json", 1024**2)
        digest = identity(
            {
                "manifest": manifest,
                "receipt_digest": dataset.receipt_digest,
                "resources": resources,
                "files": files,
            }
        )
        return InstalledPackage(package, manifest, receipt, digest)
    except CatalogError:
        raise
    except Exception:
        raise CatalogError("Installed runtime package is unavailable or invalid") from None
