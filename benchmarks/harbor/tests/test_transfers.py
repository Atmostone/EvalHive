"""Chunk receipts and real installed filesystem tools preserve native outputs."""

import base64
import hashlib
import os
from pathlib import Path

import pytest
from evalhive_harbor.archive import CHUNK_BYTES, TransferError
from evalhive_harbor.staging import operate


def test_staging_replays_matching_chunks_and_rejects_conflicting_or_noncontiguous_bytes(tmp_path):
    identity = "a" * 32
    operate(tmp_path, "create", {"id": identity})
    chunk = {"id": identity, "offset": 0, "data": base64.b64encode(b"native").decode()}
    assert operate(tmp_path, "write", chunk) == {"written": 6}
    assert operate(tmp_path, "write", chunk) == {"written": 6}
    with pytest.raises(TransferError, match="conflicts"):
        operate(tmp_path, "write", {**chunk, "data": base64.b64encode(b"other!").decode()})
    with pytest.raises(TransferError, match="invalid"):
        operate(tmp_path, "write", {**chunk, "offset": 7})
    proof = {"id": identity, "size_bytes": 6, "sha256": hashlib.sha256(b"native").hexdigest()}
    assert operate(tmp_path, "seal", proof)["sealed"]
    assert (tmp_path / (identity + ".tar")).stat().st_mode & 0o777 == 0o444
    assert operate(tmp_path, "remove", {"id": identity})["removed"]


@pytest.mark.parametrize("identity", ["../escaped", "a" * 33, "A" * 32, None])
def test_staging_rejects_foreign_paths_before_creation(tmp_path, identity):
    with pytest.raises(TransferError, match="identity"):
        operate(tmp_path, "create", {"id": identity})
    assert list(tmp_path.iterdir()) == []


def test_staging_rejects_oversized_chunks_and_invalid_archive_proofs(tmp_path):
    identity = "b" * 32
    operate(tmp_path, "create", {"id": identity})
    with pytest.raises(TransferError):
        operate(tmp_path, "write", {
            "id": identity, "offset": 0,
            "data": base64.b64encode(b"a" * (CHUNK_BYTES + 1)).decode(),
        })
    with pytest.raises(TransferError, match="proof"):
        operate(tmp_path, "seal", {"id": identity, "size_bytes": 3, "sha256": "0" * 64})




@pytest.mark.asyncio
async def test_pinned_tools_transfer_native_tests_and_filtered_protected_rewards(native_transfers, tmp_path):
    source = tmp_path / "private-tests"
    source.mkdir()
    (source / "test.sh").write_bytes(b"#!/bin/bash\nexit 0\n")
    (source / "test.sh").chmod(0o751)
    target = tmp_path / "native-tests"
    await native_transfers.upload(source_dir=source, target_dir=str(target))
    assert (target / "test.sh").read_bytes() == (source / "test.sh").read_bytes()
    assert (target / "test.sh").stat().st_mode & 0o777 == 0o751
    assert list(Path("/evalhive-stage").iterdir()) == []
    (target / "reward.json").write_text('{"native_score": 0.5}')
    (target / "discard.log").write_text("unselected")
    (target / "nested").mkdir()
    (target / "nested/report.txt").write_bytes(os.urandom(2 * CHUNK_BYTES + 97))
    received = tmp_path / "received"
    await native_transfers.download(
        source_dir=str(target), target_dir=received,
        include=["*.txt"], exclude=["reward.json"], protect=["reward.json"],
    )
    assert (received / "reward.json").read_text() == '{"native_score": 0.5}'
    assert (received / "nested/report.txt").read_bytes() == (target / "nested/report.txt").read_bytes()
    assert not (received / "discard.log").exists()
    assert not (received / "test.sh").exists()


@pytest.mark.asyncio
async def test_missing_native_download_source_is_an_explicit_transfer_failure(native_transfers, tmp_path):
    with pytest.raises(TransferError, match="filesystem operation"):
        await native_transfers.download(source_dir=str(tmp_path / "missing"), target_dir=tmp_path / "received")


@pytest.mark.asyncio
async def test_native_artifact_file_transfer_renames_and_inspects_without_copying_siblings(native_transfers, tmp_path):
    source = tmp_path / "file.txt"
    source.write_text("native file artifact")
    destination = tmp_path / "native/out.txt"
    await native_transfers.upload_file(source_path=source, target_path=str(destination))
    assert destination.read_text() == source.read_text()
    assert await native_transfers.path_is_dir(str(destination.parent))
    assert not await native_transfers.path_is_dir(str(destination))
    assert not await native_transfers.path_is_dir(str(tmp_path / "missing"))
    # A sibling FIFO must not be inspected or block a single-file transfer.
    os.mkfifo(destination.parent / "unrelated.fifo")
    received = tmp_path / "downloaded.txt"
    await native_transfers.download_file(source_path=str(destination), target_path=received)
    assert received.read_text() == source.read_text()
