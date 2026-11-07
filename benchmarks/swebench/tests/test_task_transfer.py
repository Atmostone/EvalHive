"""Capture actual Git changes, including untracked files, without evaluator mounts."""

import base64
import hashlib
import subprocess

import pytest
from evalhive_swebench import task_transfer
from evalhive_swebench.task_transfer import execute


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()

    def git(*arguments):
        return (
            subprocess.check_output(
                ["/usr/bin/git", *arguments],
                cwd=root,
                env={
                    "PATH": "/usr/bin:/bin",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
                },
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )

    git("init")
    (root / "existing.py").write_text("before = True\n")
    git("add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "Initial fixture",
    )
    return root, git("rev-parse", "HEAD")


def test_real_git_capture_preserves_new_binary_and_modified_files(repository, tmp_path):
    repo, commit = repository
    (repo / "existing.py").write_text("after = True\n")
    (repo / "new.py").write_text("new_file = True\n")
    (repo / "new.bin").write_bytes(b"\0binary\0fixture\xff")
    root, identity = tmp_path / "capture", "a" * 32
    receipt = execute("capture", {"id": identity, "base_commit": commit}, root=root, cwd=repo)
    chunk = execute("read", {"id": identity, "offset": 0}, root=root, cwd=repo)
    data = base64.b64decode(chunk["data"])
    assert receipt["size_bytes"] == len(data)
    assert receipt["sha256"] == hashlib.sha256(data).hexdigest()
    assert b"+after = True" in data and b"+new_file = True" in data
    assert b"GIT binary patch" in data
    assert (
        execute("capture", {"id": identity, "base_commit": commit}, root=root, cwd=repo) == receipt
    )
    with pytest.raises(ValueError, match="conflicts"):
        execute("capture", {"id": identity, "base_commit": "b" * 40}, root=root, cwd=repo)


def test_empty_patch_is_distinct_from_failed_capture(repository, tmp_path):
    repo, commit = repository
    root = tmp_path / "capture"
    empty = execute("capture", {"id": "a" * 32, "base_commit": commit}, root=root, cwd=repo)
    assert empty["size_bytes"] == 0
    with pytest.raises(ValueError, match="no empty patch"):
        execute("capture", {"id": "b" * 32, "base_commit": "f" * 40}, root=root, cwd=repo)
    assert not (root / ("b" * 32) / "receipt.json").exists()


def test_changed_or_linked_patch_cannot_reuse_a_capture_receipt(repository, tmp_path):
    repo, commit = repository
    root, identity = tmp_path / "capture", "c" * 32
    execute("capture", {"id": identity, "base_commit": commit}, root=root, cwd=repo)
    patch = root / identity / "patch.diff"
    patch.write_text("Changed evidence")
    with pytest.raises(ValueError, match="changed after"):
        execute("read", {"id": identity, "offset": 0}, root=root, cwd=repo)
    patch.unlink()
    patch.symlink_to(repo / "existing.py")
    with pytest.raises(ValueError, match="links"):
        execute("read", {"id": identity, "offset": 0}, root=root, cwd=repo)


def test_evidence_limit_does_not_limit_git_index_but_still_bounds_patch(
    repository, tmp_path, monkeypatch
):
    repo, commit = repository
    for index in range(30):
        (repo / f"file-{index}.py").write_text("unchanged = True\n")
    subprocess.run(["/usr/bin/git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "Larger index",
        ],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    commit = (
        subprocess.check_output(["/usr/bin/git", "rev-parse", "HEAD"], cwd=repo).decode().strip()
    )
    monkeypatch.setattr(task_transfer, "MAX_BYTES", 512)
    assert (repo / ".git/index").stat().st_size > 512
    (repo / "existing.py").write_text("after = True\n")
    result = execute(
        "capture", {"id": "d" * 32, "base_commit": commit}, root=tmp_path / "capture", cwd=repo
    )
    assert 0 < result["size_bytes"] < 512
    (repo / "existing.py").write_text("large = True\n" * 1000)
    with pytest.raises(ValueError, match="byte budget"):
        execute(
            "capture", {"id": "e" * 32, "base_commit": commit}, root=tmp_path / "capture", cwd=repo
        )
    assert not (tmp_path / "capture" / ("e" * 32) / "receipt.json").exists()
