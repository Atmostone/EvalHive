"""Server publication preserves native packages and never exposes partial catalogs."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest
from evalhive_benchmark.installation import dataset_key

ROOT = Path(__file__).parents[3]
SPEC = importlib.util.spec_from_file_location(
    "catalog_publish", ROOT / "scripts/publish-benchmark-catalog.py"
)
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)
MANIFEST = json.loads((ROOT / "benchmark-sdk/fixtures/conformance.json").read_text())["manifest"]
WORKER, TASK = "sha256:" + "a" * 64, "sha256:" + "b" * 64


def package(root, revision):
    root.mkdir()
    manifest = {**copy.deepcopy(MANIFEST), "image": WORKER}
    receipt = {
        "format_version": "1.0",
        "connector_id": manifest["id"],
        "connector_version": manifest["version"],
        "dataset_id": "native",
        "revision": revision,
        "source_digest": "sha256:" + "c" * 64,
        "worker_image": WORKER,
        "cases": [
            {
                "ref": {"dataset_id": "native", "revision": revision, "case_id": "one"},
                "title": "Native case",
                "source_digest": "sha256:" + "d" * 64,
                "native_ref": {"private": "native task locator"},
                "requirements": {},
            }
        ],
    }
    documents = {
        "manifest.json": manifest,
        "resources.json": {
            "format_version": "1.0",
            "connector_id": manifest["id"],
            "connector_version": manifest["version"],
            "provider_id": "example.provider",
            "worker_asset": "worker",
            "assets": {
                "worker": {"kind": "container", "image": WORKER},
                "task": {"kind": "container", "image": TASK},
            },
        },
        "files.json": {
            "format_version": "1.0",
            "files": {"data": {"kind": "relative", "path": "native-data"}},
        },
        "datasets/" + dataset_key("native", revision) + ".json": receipt,
    }
    for name, value in documents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    (root / "native-data").mkdir()
    (root / "native-data/private.txt").write_bytes(b"unchanged native evaluator data")
    return root


@pytest.fixture
def image_inventory(monkeypatch):
    calls = []

    def docker(*args):
        calls.append(args)
        if args[0] == "info":
            return '["name=rootless"]'
        if args[:2] == ("image", "inspect"):
            return args[2]
        assert args[:2] == ("image", "tag")
        return ""

    monkeypatch.setattr(publisher, "docker", docker)
    return calls


def test_multiple_revisions_share_one_catalog_without_changing_native_bytes(
    tmp_path, image_inventory
):
    first = package(tmp_path / "first", "old")
    second = package(tmp_path / "second", "refs/new:revision")
    destination = tmp_path / "release"
    assert publisher.publish([first, second], destination) == {
        "connectors": 1,
        "datasets": 2,
        "images": 2,
    }
    catalog = next(destination.glob("*/manifest.json")).parent
    index = json.loads((catalog / "installations.json").read_text())["installations"]
    assert set(index) == {dataset_key("native", "old"), dataset_key("native", "refs/new:revision")}
    for relative in index.values():
        assert (
            catalog / relative / "native-data/private.txt"
        ).read_bytes() == b"unchanged native evaluator data"
    assert len(list(catalog.glob("datasets/*.json"))) == 2
    assert len([call for call in image_inventory if call[:2] == ("image", "tag")]) == 2
    with pytest.raises(ValueError, match="already exists"):
        publisher.publish([first], destination)
    assert len(list(catalog.glob("datasets/*.json"))) == 2


def test_duplicate_revision_is_rejected_before_catalog_publication(tmp_path, image_inventory):
    first, second = package(tmp_path / "first", "same"), package(tmp_path / "second", "same")
    destination = tmp_path / "release"
    with pytest.raises(ValueError, match="unique dataset revisions"):
        publisher.publish([first, second], destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".benchmark-catalog-*"))


def test_copy_failure_removes_staging_and_keeps_source_packages(
    tmp_path, image_inventory, monkeypatch
):
    first, second = package(tmp_path / "first", "one"), package(tmp_path / "second", "two")
    original = publisher.shutil.copytree

    def fail_second(source, target, *args, **kwargs):
        if source == second:
            raise OSError("fixture disk failure")
        return original(source, target, *args, **kwargs)

    monkeypatch.setattr(publisher.shutil, "copytree", fail_second)
    destination = tmp_path / "release"
    with pytest.raises(OSError, match="fixture disk failure"):
        publisher.publish([first, second], destination)
    assert not destination.exists() and not list(tmp_path.glob(".benchmark-catalog-*"))
    assert (first / "native-data/private.txt").exists() and (second / "manifest.json").exists()


@pytest.mark.parametrize("invalid", ["floating_image", "outside_registration", "symlink"])
def test_unsafe_packages_fail_without_publication(tmp_path, image_inventory, invalid):
    source = package(tmp_path / "source", "one")
    if invalid == "floating_image":
        path = source / "resources.json"
        value = json.loads(path.read_text())
        value["assets"]["task"]["image"] = "native:latest"
        path.write_text(json.dumps(value))
    elif invalid == "outside_registration":
        path = source / "files.json"
        value = json.loads(path.read_text())
        value["files"]["data"]["path"] = "../outside"
        path.write_text(json.dumps(value))
    else:
        (source / "native-data/link").symlink_to(tmp_path / "outside")
    destination = tmp_path / "release"
    with pytest.raises(ValueError):
        publisher.publish([source], destination)
    assert not destination.exists()
