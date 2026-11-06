"""Installed file maps cannot introduce host paths or writable runtime bindings."""

from copy import deepcopy
from pathlib import Path

import pytest
from app.runtime.managed_spec import ManagedRuntimeError, PathMapping, RuntimePolicy
from app.runtime.registered_assets import RegisteredFile
from app.runtime.registered_files import load_registered_files


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "package"
    root.mkdir()
    (root / "cases").mkdir()
    (root / "cases/task.json").write_text('{"prompt":"Public task"}')
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    mapping = PathMapping("/installed/package", str(root))
    policy = RuntimePolicy(
        PathMapping("/owned", "/owned"),
        (mapping, PathMapping("/installed/bundle", str(bundle))),
    )
    document = {
        "format_version": "1.0",
        "files": {
            "cases": {"kind": "relative", "path": "cases", "source_digest": "sha256:" + "a" * 64},
            "agent": {"kind": "runtime_binding", "name": "harness.runtime"},
        },
    }
    bindings = {"harness.runtime": RegisteredFile("/installed/bundle", "sha256:" + "b" * 64)}
    return mapping, policy, document, bindings


def test_data_only_installation_resolves_contained_files_and_host_owned_runtime(installation):
    mapping, policy, document, bindings = installation
    grants = load_registered_files(document, mapping, policy, bindings=bindings)
    assert grants["cases"].host_path == "/installed/package/cases"
    assert grants["agent"] == bindings["harness.runtime"]
    assert not any(grant.writable for grant in grants.values())


@pytest.mark.parametrize(
    "path",
    [
        "/etc",
        "../bundle",
        "cases/../../bundle",
        "cases/../cases",
        "cases//task.json",
        ".",
        "cases\\task.json",
    ],
)
def test_relative_files_cannot_escape_or_use_ambiguous_paths(installation, path):
    mapping, policy, document, bindings = installation
    document["files"]["cases"]["path"] = path
    with pytest.raises(ManagedRuntimeError):
        load_registered_files(document, mapping, policy, bindings=bindings)


def test_links_unknown_properties_and_missing_or_writable_bindings_fail_closed(installation):
    mapping, policy, document, bindings = installation
    (Path(mapping.local) / "linked").symlink_to("cases", target_is_directory=True)
    for replacement in (
        {**document["files"]["cases"], "path": "linked"},
        {**document["files"]["cases"], "writable": True},
        {"kind": "runtime_binding", "name": "missing"},
        {"kind": "host_path", "path": "/etc"},
    ):
        changed = deepcopy(document)
        changed["files"]["cases"] = replacement
        with pytest.raises(ManagedRuntimeError):
            load_registered_files(changed, mapping, policy, bindings=bindings)
    bindings["harness.runtime"] = RegisteredFile(
        "/installed/bundle", "sha256:" + "b" * 64, writable=True
    )
    with pytest.raises(ManagedRuntimeError):
        load_registered_files(document, mapping, policy, bindings=bindings)
