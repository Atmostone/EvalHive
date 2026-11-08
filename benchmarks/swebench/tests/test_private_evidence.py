"""Protected storage accepts only the patch and log evidence it confirmed."""

import base64
import hashlib
import json
import tarfile

import pytest
from evalhive_benchmark.world_rpc import WorldError
from evalhive_swebench.eval_transfer import execute
from evalhive_swebench.world_service import NativeService


def service(tmp_path, effect):
    paths = {
        name: tmp_path / name for name in ("stage", "records", "checkpoint", "captured", "logs")
    }
    for path in paths.values():
        path.mkdir(parents=True)
    value = NativeService(None, effect, **paths)
    value.case = {"source_digest": "sha256:" + "a" * 64}
    value.instance = {"base_commit": "b" * 40}
    value.image_commit = "b" * 40
    return value


def test_capture_checks_actual_shared_evidence_before_publishing_private_checkpoint(tmp_path):
    data = b"Engineering patch\n"

    def effect(operation, payload):
        assert operation == "exec" and payload["resource"] == "task"
        command = payload["argv"]
        assert command[2] == "capture"
        request = json.loads(base64.b64decode(command[3]))
        output = tmp_path / "captured" / request["id"]
        output.mkdir()
        (output / "patch.diff").write_bytes(data)
        proof = {**request, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        return {"exit_code": 0, "stdout": json.dumps(proof)}

    world = service(tmp_path, effect)
    proof = world.capture()
    assert world.capture() == proof
    assert (world.checkpoint / "patch.diff").read_bytes() == data
    assert not list(world.records.iterdir())
    assert proof["source_digest"] == world.case["source_digest"]


def test_cold_restore_rejects_another_case_or_changed_patch_and_accepts_frozen_checkpoint(tmp_path):
    original = service(tmp_path / "original", None)
    proof = original.save_patch(b"Original patch\n")
    archive = tmp_path / "checkpoint.tar"
    with tarfile.open(archive, "w") as stream:
        stream.add(original.checkpoint, arcname="checkpoint")
    restored = service(tmp_path / "restored", None)
    restored.restore(archive, proof)
    assert restored.captured == proof and restored.patch == "Original patch\n"
    another = service(tmp_path / "another", None)
    another.case = {"source_digest": "sha256:" + "c" * 64}
    with pytest.raises(WorldError, match="differs from this task"):
        another.restore(archive, proof)
    (original.checkpoint / "patch.diff").write_bytes(b"Changed patch\n")
    with tarfile.open(archive, "w") as stream:
        stream.add(original.checkpoint, arcname="checkpoint")
    changed = service(tmp_path / "changed", None)
    with pytest.raises(WorldError, match="differs from this task"):
        changed.restore(archive, proof)


def test_private_log_integrity_and_evaluator_patch_identity_are_enforced(tmp_path):
    world = service(tmp_path / "world", None)
    data = b"Original test evidence\n"
    (world.logs / "test_output.txt").write_bytes(data)
    (world.logs / "receipt.json").write_text(
        json.dumps({"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    )
    assert world.read_log() == data
    (world.logs / "test_output.txt").write_bytes(b"Changed test evidence\n")
    with pytest.raises(WorldError, match="differs from its receipt"):
        world.read_log()
    stage, root, patch = tmp_path / "stage", tmp_path / "results", tmp_path / "patch.diff"
    stage.mkdir()
    (stage / "patch.diff").write_bytes(b"Private model patch\n")
    with pytest.raises(ValueError, match="another patch"):
        execute("prepare", "0" * 64, stage=stage, root=root, patch=patch)
    digest = hashlib.sha256((stage / "patch.diff").read_bytes()).hexdigest()
    assert execute("prepare", digest, stage=stage, root=root, patch=patch)["prepared"]
    assert patch.read_bytes() == b"Private model patch\n"
    (root / "unexpected").write_text("Existing state")
    with pytest.raises(ValueError, match="fresh private"):
        execute("prepare", digest, stage=stage, root=root, patch=patch)
