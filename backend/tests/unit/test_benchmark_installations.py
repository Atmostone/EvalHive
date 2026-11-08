"""One connector registers distinct native packages without backend name branches."""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.benchmarks.catalog import CatalogError
from app.benchmarks.datasets import installed_dataset
from app.benchmarks.installations import resolve_package
from evalhive_benchmark.installation import dataset_key, installation_digest

from tests.unit.test_benchmark_catalog import manifest


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def registry(tmp_path):
    catalog = tmp_path / "example"
    declaration = manifest()
    write(catalog / "manifest.json", declaration)
    datasets, index = [], {}
    for variant in ("alpha", "beta"):
        dataset_id, revision = "fixture-" + variant, "original/" + variant
        key = dataset_key(dataset_id, revision)
        receipt = {
            "format_version": "1.0",
            "connector_id": declaration["id"],
            "connector_version": declaration["version"],
            "dataset_id": dataset_id,
            "revision": revision,
            "source_digest": "sha256:" + hashlib.sha256(variant.encode()).hexdigest(),
            "worker_image": declaration["image"],
            "cases": [
                {
                    "ref": {
                        "dataset_id": dataset_id,
                        "revision": revision,
                        "case_id": "original-case",
                    },
                    "title": "Original task",
                    "source_digest": "sha256:" + "d" * 64,
                    "native_ref": {},
                    "requirements": {},
                }
            ],
        }
        package = catalog / "packages" / variant
        write(catalog / "datasets" / (key + ".json"), receipt)
        write(package / "datasets" / (key + ".json"), receipt)
        write(package / "manifest.json", declaration)
        write(package / "resources.json", {"variant": variant})
        write(package / "files.json", {"files": {}})
        (package / "worker.py").write_text("raise RuntimeError('native code must not be imported')")
        index[key] = "packages/" + variant
        datasets.append(
            SimpleNamespace(
                connector_id=declaration["id"],
                connector_version=declaration["version"],
                dataset_id=dataset_id,
                revision=revision,
                worker_image=declaration["image"],
                manifest=deepcopy(declaration),
                receipt_digest=installation_digest(receipt),
            )
        )
    write(catalog / "installations.json", {"format_version": "1.0", "installations": index})
    return tmp_path, catalog, datasets


def test_different_dataset_packages_share_one_connector_and_keep_native_identity(registry):
    root, catalog, datasets = registry
    alpha, beta = [resolve_package(row, directory=root) for row in datasets]
    assert alpha.directory == catalog / "packages/alpha"
    assert beta.directory == catalog / "packages/beta"
    assert alpha.manifest == beta.manifest
    assert alpha.receipt["revision"] != beta.receipt["revision"]
    assert alpha.metadata_digest != beta.metadata_digest
    write(alpha.directory / "resources.json", {"variant": "changed"})
    assert resolve_package(datasets[0], directory=root).metadata_digest != alpha.metadata_digest


def test_direct_packages_remain_compatible_without_an_index(registry):
    root, catalog, datasets = registry
    package = catalog / "packages/alpha"
    (catalog / "installations.json").unlink()
    for name in ("resources.json", "files.json"):
        (catalog / name).write_bytes((package / name).read_bytes())
    assert resolve_package(datasets[0], directory=root).directory == catalog


@pytest.mark.parametrize(
    "relative", ["../outside", "/tmp", "packages//alpha", "packages/alpha/.", "packages\\alpha"]
)
def test_runtime_packages_cannot_escape_or_ambiguously_name_installed_paths(registry, relative):
    root, catalog, datasets = registry
    key = dataset_key(datasets[0].dataset_id, datasets[0].revision)
    write(
        catalog / "installations.json", {"format_version": "1.0", "installations": {key: relative}}
    )
    with pytest.raises(CatalogError, match="contained"):
        resolve_package(datasets[0], directory=root)


def test_unregistered_runtime_keeps_the_case_catalog_but_cannot_run(registry):
    root, catalog, datasets = registry
    row = datasets[0]
    write(catalog / "installations.json", {"format_version": "1.0", "installations": {}})
    assert (
        len(
            installed_dataset(
                row.connector_id,
                row.connector_version,
                row.dataset_id,
                row.revision,
                directory=root,
            )[1]["cases"]
        )
        == 1
    )
    with pytest.raises(CatalogError, match="not registered"):
        resolve_package(row, directory=root)


def test_package_links_and_changed_receipts_or_manifests_fail_closed(registry):
    root, catalog, datasets = registry
    row = datasets[0]
    package = catalog / "packages/alpha"
    destination = catalog / "packages/elsewhere"
    package.rename(destination)
    package.symlink_to(destination, target_is_directory=True)
    with pytest.raises(CatalogError, match="inside"):
        resolve_package(row, directory=root)
    package.unlink()
    destination.rename(package)
    key = dataset_key(row.dataset_id, row.revision)
    receipt_path = package / "datasets" / (key + ".json")
    receipt = json.loads(receipt_path.read_text())
    receipt["cases"][0]["title"] = "Changed after registration"
    write(receipt_path, receipt)
    with pytest.raises(CatalogError, match="differs"):
        resolve_package(row, directory=root)
    write(receipt_path, json.loads((catalog / "datasets" / (key + ".json")).read_text()))
    altered = deepcopy(row.manifest)
    altered["entrypoint"] = ["another-worker"]
    write(package / "manifest.json", altered)
    with pytest.raises(CatalogError, match="differs"):
        resolve_package(row, directory=root)
