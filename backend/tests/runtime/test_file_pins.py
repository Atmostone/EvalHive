"""Replay proofs cover installed bytes, layout and executable permissions."""

from dataclasses import replace

import pytest
from app.runtime.file_pins import content_proof, freeze_files, installed_path
from app.runtime.managed_spec import ManagedRuntimeError, PathMapping, RuntimePolicy
from app.runtime.registered_assets import RegisteredFile


def test_file_tree_proof_detects_content_names_modes_and_empty_directories(tmp_path):
    root = tmp_path / "installed"
    root.mkdir()
    value = root / "worker"
    value.write_bytes(b"fixture bytes")
    first = content_proof(root)
    assert first["size_bytes"] == len(b"fixture bytes") and first["entries"] == 2
    assert content_proof(root) == first
    value.write_bytes(b"modified byte")
    assert content_proof(root) != first
    value.write_bytes(b"fixture bytes")
    assert content_proof(root) == first
    value.chmod(0o755)
    executable = content_proof(root)
    assert executable != first
    (root / "empty").mkdir()
    assert content_proof(root) != executable


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_file_tree_proof_rejects_links(tmp_path, kind):
    root, outside = tmp_path / "installed", tmp_path / "outside"
    root.mkdir()
    outside.mkdir() if kind == "directory" else outside.write_bytes(b"not installed")
    (root / "linked").symlink_to(outside)
    with pytest.raises(ManagedRuntimeError, match="unsupported"):
        content_proof(root)


@pytest.mark.parametrize("maximum,limit", [("MAX_ENTRIES", 1), ("MAX_BYTES", 2)])
def test_file_proofs_are_bounded_before_unlimited_scanning(tmp_path, maximum, limit, monkeypatch):
    (tmp_path / "file").write_bytes(b"fixture")
    monkeypatch.setattr("app.runtime.file_pins." + maximum, limit)
    with pytest.raises(ManagedRuntimeError, match="policy"):
        content_proof(tmp_path)


def test_only_registered_readonly_installations_are_replay_sources(tmp_path):
    data, installed = tmp_path / "data", tmp_path / "installed"
    data.mkdir()
    installed.mkdir()
    (installed / "worker").write_bytes(b"fixture")
    policy = RuntimePolicy(
        PathMapping("/host/data", str(data)), (PathMapping("/host/install", str(installed)),)
    )
    grant = RegisteredFile("/host/install/worker", "sha256:" + "a" * 64)
    saved = freeze_files(
        {
            "worker": grant,
            "mutable": replace(grant, writable=True),
            "old-world": replace(grant, host_path="/host/data/old"),
        },
        policy,
    )
    assert installed_path(grant.host_path, policy) == installed / "worker"
    assert saved["worker"]["content_proof"]["size_bytes"] == 7
    assert saved["mutable"]["content_proof"] is None and saved["old-world"]["content_proof"] is None
    assert saved["worker"]["source_digest"] == grant.source_digest
    with pytest.raises(ManagedRuntimeError, match="read-only"):
        installed_path("/host/data/old", policy)


def test_snapshot_bounds_apply_across_grants_and_shared_paths_are_hashed_once(
    tmp_path, monkeypatch
):
    (tmp_path / "first").write_bytes(b"1234")
    (tmp_path / "second").write_bytes(b"5678")
    policy = RuntimePolicy(
        PathMapping("/data", "/data"), (PathMapping("/installed", str(tmp_path)),)
    )
    grant = RegisteredFile("/installed/first", "sha256:" + "a" * 64)
    monkeypatch.setattr("app.runtime.file_pins.MAX_BYTES", 4)
    saved = freeze_files(
        {"a": grant, "b": grant, "c": replace(grant, host_path="/installed/second")}, policy
    )
    assert saved["a"]["content_proof"] == saved["b"]["content_proof"]
    assert saved["a"]["content_proof"] is not None and saved["c"]["content_proof"] is None
