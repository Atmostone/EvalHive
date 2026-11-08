"""Pinned dataset imports are scoped, idempotent and immutable in PostgreSQL."""

import copy
import json
import uuid

import pytest
from app.benchmarks.catalog import CatalogError, public_case
from app.benchmarks.datasets import DatasetConflict, installed_dataset, register_dataset
from app.config import get_settings
from app.models.benchmark import BenchmarkDatasetCase, BenchmarkDatasetRevision, BenchmarkProfile
from evalhive_benchmark.installation import dataset_key
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture
def installed_dataset_fixture(tmp_path, monkeypatch):
    package = tmp_path / "example"
    (package / "datasets").mkdir(parents=True)
    manifest = {"protocol_version": "1.0", "kind": "benchmark", "id": "example", "version": "0.1.0",
        "display_name": "Example", "entrypoint": ["python", "worker.py"], "image": "image@sha256:" + "a" * 64,
        "config_schema": {"type": "object"}, "capabilities": {}, "requirements": {}}
    (package / "manifest.json").write_text(json.dumps(manifest))
    receipt = {"format_version": "1.0", "connector_id": "example", "connector_version": "0.1.0",
        "dataset_id": "fixture", "revision": "native/v1", "source_digest": "sha256:" + "b" * 64,
        "worker_image": manifest["image"], "cases": [{
            "ref": {"dataset_id": "fixture", "revision": "native/v1", "case_id": "one"},
            "title": "Public case", "source_digest": "sha256:" + "c" * 64,
            "native_ref": {"hidden_tests": "/evaluator/private"},
            "metadata": {"expected_answer": "hidden"},
            "requirements": {"resources": {"memory_mb": 4096}}}]}
    path = package / "datasets" / (dataset_key("fixture", "native/v1") + ".json")
    path.write_text(json.dumps(receipt))
    monkeypatch.setattr(get_settings(), "benchmark_manifest_dir", str(tmp_path))
    return path, receipt


async def profile(db):
    item = BenchmarkProfile(workspace_id=WORKSPACE, name="Native example", connector_id="example",
        connector_version="0.1.0", config={}, secret_refs={}, model_bindings={})
    db.add(item)
    await db.flush()
    return item


async def test_revision_import_preserves_native_data_and_rejects_drift(db_session, installed_dataset_fixture):
    selected = await profile(db_session)
    dataset = await register_dataset(db_session, WORKSPACE, selected, "fixture", "native/v1")
    assert (await register_dataset(db_session, WORKSPACE, selected, "fixture", "native/v1")).id == dataset.id
    case = await db_session.scalar(select(BenchmarkDatasetCase).where(BenchmarkDatasetCase.dataset_revision_id == dataset.id))
    assert case.descriptor["native_ref"]["hidden_tests"] == "/evaluator/private"
    assert public_case(case.descriptor)["requirements"]["resources"]["memory_mb"] == 4096
    assert "hidden" not in json.dumps(public_case(case.descriptor))
    path, receipt = installed_dataset_fixture
    changed = copy.deepcopy(receipt)
    changed["cases"][0]["title"] = "Changed after install"
    path.write_text(json.dumps(changed))
    with pytest.raises(DatasetConflict, match="different content"):
        await register_dataset(db_session, WORKSPACE, selected, "fixture", "native/v1")


async def test_postgresql_rejects_revision_and_case_updates(db_session, installed_dataset_fixture):
    selected = await profile(db_session)
    dataset = await register_dataset(db_session, WORKSPACE, selected, "fixture", "native/v1")
    identity = dataset.id
    await db_session.commit()
    dataset.revision = "silently changed"
    with pytest.raises(DBAPIError, match="immutable"):
        await db_session.flush()
    await db_session.rollback()
    case = await db_session.scalar(select(BenchmarkDatasetCase).where(BenchmarkDatasetCase.dataset_revision_id == identity))
    case.descriptor = {**case.descriptor, "title": "changed"}
    with pytest.raises(DBAPIError, match="immutable"):
        await db_session.flush()
    await db_session.rollback()
    assert (await db_session.get(BenchmarkDatasetRevision, identity)).revision == "native/v1"


async def test_dataset_registration_checks_workspace_before_loading_private_data(db_session, installed_dataset_fixture):
    selected = await profile(db_session)
    with pytest.raises(CatalogError, match="another workspace"):
        await register_dataset(db_session, uuid.uuid4(), selected, "fixture", "native/v1")


def test_installation_cannot_escape_its_registry_directory(installed_dataset_fixture):
    path, receipt = installed_dataset_fixture
    path.unlink()
    outside = path.parent.parent.parent / "outside.json"
    outside.write_text(json.dumps(receipt))
    path.symlink_to(outside)
    with pytest.raises(CatalogError, match="bounded installed"):
        installed_dataset("example", "0.1.0", "fixture", "native/v1")


@pytest.mark.parametrize("prefix", ["sha256:", "image@sha256:"])
def test_pinned_worker_identity_must_match_dataset_receipt(installed_dataset_fixture, prefix):
    path, receipt = installed_dataset_fixture
    manifest_path = path.parent.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["image"] = prefix + "a" * 64
    receipt["worker_image"] = prefix + "b" * 64
    path.write_text(json.dumps(receipt))
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(CatalogError, match="worker image does not match"):
        installed_dataset("example", "0.1.0", "fixture", "native/v1")
