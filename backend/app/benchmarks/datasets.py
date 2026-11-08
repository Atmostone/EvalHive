"""Import administrator-installed metadata without executing a connector in the API."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from evalhive_benchmark import ProtocolError
from evalhive_benchmark.installation import dataset_key, installation_digest, validate_installation
from sqlalchemy import insert, select

from app.benchmarks.catalog import CatalogError, get_manifest
from app.config import get_settings
from app.models.benchmark import BenchmarkDatasetCase, BenchmarkDatasetRevision
from app.models.workspace import Workspace


class DatasetConflict(ValueError):
    pass


def installed_dataset(connector_id: str, version: str, dataset_id: str, revision: str, *, directory=None):
    root = Path(directory or get_settings().benchmark_manifest_dir)
    manifest = get_manifest(connector_id, version, directory=root)
    try:
        package = next(path.parent for path in root.glob("*/manifest.json")
                       if (value := json.loads(path.read_text()))["id"] == connector_id
                       and value["kind"] == "benchmark")
        path = package / "datasets" / (dataset_key(dataset_id, revision) + ".json")
        if (path.is_symlink() or path.parent.is_symlink()
                or not path.resolve().is_relative_to(package.resolve())
                or path.stat().st_size > 64 * 1024 * 1024):
            raise CatalogError("Dataset installation must be a bounded installed file")
        receipt = validate_installation(json.loads(path.read_bytes()))
        if (receipt["connector_id"], receipt["connector_version"], receipt["dataset_id"], receipt["revision"]) != (
            connector_id, version, dataset_id, revision
        ):
            raise CatalogError("Dataset installation identity does not match the request")
        pinned = manifest["image"].startswith("sha256:") or "@sha256:" in manifest["image"]
        if pinned and manifest["image"] != receipt["worker_image"]:
            raise CatalogError("Dataset worker image does not match the installed connector")
    except CatalogError:
        raise
    except (OSError, ValueError, ProtocolError, StopIteration, RecursionError):
        raise CatalogError("Dataset revision is not installed or its metadata is invalid") from None
    return manifest, receipt


async def register_dataset(db, workspace_id, profile, dataset_id: str, revision: str):
    if profile.workspace_id != workspace_id:
        raise CatalogError("Benchmark profile belongs to another workspace")
    await db.execute(select(Workspace.id).where(Workspace.id == workspace_id).with_for_update())
    manifest, receipt = installed_dataset(profile.connector_id, profile.connector_version, dataset_id, revision)
    key, digest = dataset_key(dataset_id, revision), installation_digest(receipt)
    existing = await db.scalar(select(BenchmarkDatasetRevision).where(
        BenchmarkDatasetRevision.workspace_id == workspace_id,
        BenchmarkDatasetRevision.connector_id == profile.connector_id,
        BenchmarkDatasetRevision.connector_version == profile.connector_version,
        BenchmarkDatasetRevision.dataset_key == key,
    ))
    if existing:
        if existing.receipt_digest != digest or existing.manifest != manifest:
            raise DatasetConflict("Dataset revision already exists with different content")
        return existing
    dataset = BenchmarkDatasetRevision(workspace_id=workspace_id, connector_id=profile.connector_id,
        connector_version=profile.connector_version, dataset_id=dataset_id, revision=revision,
        dataset_key=key, source_digest=receipt["source_digest"], receipt_digest=digest,
        worker_image=receipt["worker_image"], manifest=manifest, provenance=receipt.get("provenance", {}),
        case_count=len(receipt["cases"]))
    db.add(dataset)
    await db.flush()
    for offset in range(0, len(receipt["cases"]), 500):
        rows = []
        for position, descriptor in enumerate(receipt["cases"][offset:offset + 500], start=offset):
            identity = [descriptor["ref"].get("split"), descriptor["ref"]["case_id"]]
            rows.append({"id": uuid.uuid4(), "dataset_revision_id": dataset.id, "position": position,
                "case_key": hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest(),
                "descriptor": descriptor})
        await db.execute(insert(BenchmarkDatasetCase), rows)
    return dataset
