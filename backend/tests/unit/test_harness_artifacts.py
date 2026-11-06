"""Artifact policy is independent of the harness and preserves partial failures."""

import hashlib
from types import SimpleNamespace

import pytest
from app.harnesses.artifacts import artifact_policy, collect_artifacts


class Storage:
    def __init__(self, fail=False):
        self.objects = {}
        self.fail = fail

    def bucket_exists(self, bucket):
        return True

    def put_object(self, bucket, uri, stream, size, **metadata):
        if self.fail:
            raise OSError("Storage unavailable")
        self.objects[uri] = stream.read()
        assert len(self.objects[uri]) == size


@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled"])
def test_files_survive_every_outcome_with_attempt_identity(tmp_path, outcome):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "result.txt").write_text("Result")
    (tmp_path / "private").mkdir()
    (tmp_path / "private/credentials.json").write_text("Never collect this")
    storage = Storage()
    result, events = collect_artifacts(
        SimpleNamespace(workspace=workspace),
        {"task_id": "task", "attempt_id": "attempt"},
        {"status": outcome, "summary": "Finished"},
        client=storage,
    )
    assert result["status"] == outcome
    artifact = result["metadata"]["artifacts"][0]
    assert artifact["s3_path"] == "results/task/attempts/attempt/result.txt"
    assert artifact["sha256"] == hashlib.sha256(b"Result").hexdigest()
    assert storage.objects == {artifact["s3_path"]: b"Result"}
    assert events[0]["metadata"]["delivery"] == "complete"


def test_upload_failure_is_explicit_and_keeps_native_outcome(tmp_path):
    (tmp_path / "result.txt").write_text("Result")
    result, events = collect_artifacts(
        SimpleNamespace(workspace=tmp_path),
        {"task_id": "t", "attempt_id": "a"},
        {"status": "success", "summary": "Native success"},
        client=Storage(fail=True),
    )
    assert result["status"] == "failed" and result["error"]["code"] == "ARTIFACT_DELIVERY"
    assert result["metadata"]["connector_result"]["status"] == "success"
    assert events[0]["metadata"]["delivery"] == "failed"


def test_symlinks_never_read_private_files(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private = tmp_path / "private.txt"
    private.write_text("Private data")
    (workspace / "linked.txt").symlink_to(private)
    storage = Storage()
    result, _ = collect_artifacts(
        SimpleNamespace(workspace=workspace),
        {"task_id": "t", "attempt_id": "a"},
        {"status": "success", "summary": "Finished"},
        client=storage,
    )
    assert result["status"] == "failed" and storage.objects == {}


def test_environment_root_is_not_changed_by_the_harness(tmp_path):
    (tmp_path / "output").mkdir()
    (tmp_path / "output/result.txt").write_text("Output")
    (tmp_path / "outside.txt").write_text("Outside selected root")
    storage = Storage()
    result, _ = collect_artifacts(
        SimpleNamespace(workspace=tmp_path),
        {"task_id": "t", "attempt_id": "a", "environment": {"artifact_policy": {"root": "output"}}},
        {"status": "success", "summary": "Finished"},
        client=storage,
    )
    assert [record["path"] for record in result["metadata"]["artifacts"]] == ["output/result.txt"]


def test_announced_missing_file_is_explicit(tmp_path):
    result, _ = collect_artifacts(
        SimpleNamespace(workspace=tmp_path),
        {"task_id": "t", "attempt_id": "a", "cwd": "/workspace"},
        {
            "status": "success",
            "summary": "Done",
            "metadata": {"declared_artifacts": ["missing.txt"]},
        },
        client=Storage(),
    )
    assert result["status"] == "failed"
    assert result["metadata"]["artifact_errors"] == [
        {"path": "missing.txt", "code": "DECLARED_FILE_NOT_PRESERVED"}
    ]


@pytest.mark.parametrize(
    "policy",
    [
        {"root": "../private"},
        {"root": "/tmp"},
        {"max_bytes": 0},
        {"unexpected": True},
        {"recursive": "yes"},
    ],
)
def test_invalid_artifact_policy_is_rejected(policy):
    with pytest.raises(ValueError):
        artifact_policy({"environment": {"artifact_policy": policy}})
