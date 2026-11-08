"""Installed catalogs bind bundle images and preserve earlier release catalogs."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "harness_install", Path(__file__).resolve().parents[1] / "install-harness-manifests.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def source(tmp_path):
    root = tmp_path / "source"
    path = root / "pi/manifest.json"
    path.parent.mkdir(parents=True)
    original = {
        "id": "pi",
        "version": "0.2.1",
        "image": "agent:native",
        "requirements": {
            "runtime_bundle": {"image": "bundle:native", "path": "/bundle"},
            "environment_images": {"workspace": "environment:native"},
        },
    }
    path.write_text(json.dumps(original))
    return root, path, original


def test_bundle_pins_are_immutable_per_release_and_match_inventory(tmp_path, monkeypatch):
    root, path, original = source(tmp_path)
    images = {
        "agent:native": "sha256:" + "a" * 64,
        "bundle:native": "sha256:" + "b" * 64,
        "environment:native": "sha256:" + "c" * 64,
    }
    monkeypatch.setattr(installer, "docker", lambda *args: '["name=rootless"]')
    monkeypatch.setattr(installer, "image_id", images.get)
    first = tmp_path / "release-one"
    assert installer.install(root, first) == 1
    content = (first / "pi/manifest.json").read_bytes()
    installed = json.loads(content)
    assert installed["image"] == images["agent:native"]
    assert installed["requirements"]["runtime_bundle"]["image"] == images["bundle:native"]
    assert (
        installed["requirements"]["environment_images"]["workspace"] == images["environment:native"]
    )
    inventory = json.loads((first / "images.json").read_text())["pi"]
    assert inventory["runtime_bundle"] == {
        "reference": "bundle:native",
        "image_id": images["bundle:native"],
    }
    assert inventory["installed_manifest_sha256"] == hashlib.sha256(content).hexdigest()
    images["bundle:native"] = "sha256:" + "d" * 64
    assert installer.install(root, tmp_path / "release-two") == 1
    assert (first / "pi/manifest.json").read_bytes() == content
    with pytest.raises(ValueError, match="already exists"):
        installer.install(root, first)
    assert json.loads(path.read_text()) == original


def test_missing_bundle_does_not_publish_a_partial_catalog(tmp_path, monkeypatch):
    root, _, _ = source(tmp_path)
    monkeypatch.setattr(installer, "docker", lambda *args: '["name=rootless"]')
    monkeypatch.setattr(
        installer, "image_id", lambda ref: "sha256:" + "a" * 64 if ref == "agent:native" else None
    )
    destination = tmp_path / "not-published"
    with pytest.raises(ValueError, match="runtime bundle"):
        installer.install(root, destination)
    assert not destination.exists()
    assert list(tmp_path.glob("not-published.staging-*")) == []
