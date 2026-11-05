"""Installed declarations and native requirements never execute plugin code."""

import json

import pytest
from app.benchmarks.catalog import (
    CatalogError,
    check_schema,
    compatibility,
    get_manifest,
    load_catalog,
    merged_requirements,
    public_case,
)


def manifest():
    return {"protocol_version": "1.0", "kind": "benchmark", "id": "external.example",
            "version": "0.1.0", "display_name": "Example", "entrypoint": ["python", "worker.py"],
            "image": "example@sha256:" + "a" * 64,
            "config_schema": {"type": "object", "additionalProperties": False},
            "secret_schema": {"type": "object", "properties": {"api_key": {"type": "string"}}},
            "capabilities": {"prepare": "native"}, "requirements": {"capabilities": []}}


def install(root, value, name="example"):
    package = root / name
    package.mkdir()
    (package / "manifest.json").write_text(json.dumps(value))
    (package / "worker.py").write_text("raise RuntimeError('must not import benchmark')")


def test_discovery_is_data_only_with_exact_versions(tmp_path):
    install(tmp_path, manifest())
    assert get_manifest("external.example", "0.1.0", directory=tmp_path) == manifest()
    result = load_catalog(tmp_path)
    result["external.example"]["config_schema"]["type"] = "changed"
    assert load_catalog(tmp_path)["external.example"] == manifest()
    with pytest.raises(CatalogError, match="Pinned"):
        get_manifest("external.example", "other", directory=tmp_path)
    with pytest.raises(CatalogError, match="not installed"):
        get_manifest("absent", directory=tmp_path)


def test_duplicate_ids_and_symlinked_manifests_fail_closed(tmp_path):
    install(tmp_path, manifest())
    install(tmp_path, manifest(), "duplicate")
    with pytest.raises(CatalogError, match="Duplicate"):
        load_catalog(tmp_path)
    path = tmp_path / "duplicate/manifest.json"
    path.unlink()
    path.symlink_to(tmp_path / "example/manifest.json")
    with pytest.raises(CatalogError, match="bounded installed"):
        load_catalog(tmp_path)


@pytest.mark.parametrize("schema,secret", [
    ({"$ref": "https://example.invalid/schema"}, False),
    ({"properties": {"token": {"writeOnly": True}}}, False),
    ({"properties": {"password": {"default": "never expose"}}}, True),
    ({"properties": {"key": {"examples": ["never expose"]}}}, True),
])
def test_unsafe_schemas_do_not_expose_values(schema, secret):
    with pytest.raises(CatalogError) as caught:
        check_schema(schema, secret=secret)
    assert "never expose" not in str(caught.value)


def test_native_requirements_are_preserved_and_unavailable_is_explicit():
    required = merged_requirements(
        {"capabilities": ["harness.conversation"], "resources": {"memory_mb": 512}},
        {"capabilities": ["provider.vm"], "resources": {"memory_mb": 4096, "disk_mb": 30000}},
    )
    assert required["resources"] == {"memory_mb": 4096, "disk_mb": 30000}
    admission = compatibility(required, {"capabilities": ["harness.conversation"],
        "resources": {"memory_mb": 2048, "disk_mb": 15000}})
    assert not admission["compatible"]
    assert len(admission["reasons"]) == 3
    assert admission["requirements"] == required
    assert compatibility(required, {"capabilities": required["capabilities"],
        "resources": required["resources"]})["compatible"]


def test_platform_conflicts_and_unknown_platform_are_not_compatible():
    platform = {"os": "linux", "architecture": "amd64", "libc": "glibc"}
    assert not compatibility({"platform": platform}, {})["compatible"]
    with pytest.raises(CatalogError, match="conflict"):
        merged_requirements({"platform": platform}, {"platform": {"os": "linux", "architecture": "arm64"}})


def test_public_case_omits_all_native_and_hidden_metadata():
    descriptor = {"ref": {"dataset_id": "dataset", "revision": "v1", "case_id": "one"},
        "title": "Public title", "source_digest": "sha256:" + "a" * 64, "requirements": {},
        "native_ref": {"hidden_path": "/private/answers"}, "metadata": {"gold": "private answer"}}
    public = public_case(descriptor)
    assert "native_ref" not in public and "metadata" not in public
    assert "private" not in json.dumps(public)
