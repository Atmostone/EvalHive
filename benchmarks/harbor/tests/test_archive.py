"""Native checker uploads and untrusted task downloads have bounded archives."""

import io
import tarfile

import pytest
from evalhive_harbor.archive import TransferError, pack_directory, unpack_directory


def test_native_upload_preserves_bytes_modes_and_normalizes_archive_ownership(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "nested").mkdir(mode=0o755)
    script = source / "nested" / "test.sh"
    script.write_bytes(b"#!/bin/bash\nprintf native\\n\n")
    script.chmod(0o751)
    packed = tmp_path / "archive.tar"
    receipt = pack_directory(source, packed)
    assert receipt["size_bytes"] == packed.stat().st_size
    with tarfile.open(packed) as archive:
        assert all(member.uid == member.gid == 0 for member in archive)
    unpack_directory(packed, tmp_path / "result")
    actual = tmp_path / "result/nested/test.sh"
    assert actual.read_bytes() == script.read_bytes()
    assert actual.stat().st_mode & 0o777 == 0o751


def test_native_upload_rejects_large_logical_files_and_links(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "large").write_bytes(b"a" * 1025)
    with pytest.raises(TransferError, match="logical"):
        pack_directory(source, tmp_path / "large.tar", limit=1024)
    (source / "large").unlink()
    (source / "link").symlink_to("missing")
    with pytest.raises(TransferError, match="unsupported"):
        pack_directory(source, tmp_path / "link.tar")


def archive_with(tmp_path, entries):
    result = tmp_path / "input.tar"
    with tarfile.open(result, "w") as archive:
        for name, kind, target in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            if kind == tarfile.REGTYPE:
                member.size = 2
                archive.addfile(member, io.BytesIO(b"ok"))
            else:
                member.linkname = target
                archive.addfile(member)
    return result


@pytest.mark.parametrize("name,kind,target", [
    ("../escaped", tarfile.REGTYPE, ""),
    ("/escaped", tarfile.REGTYPE, ""),
    ("fifo", tarfile.FIFOTYPE, ""),
    ("link", tarfile.SYMTYPE, "../escaped"),
])
def test_invalid_archive_is_rejected_before_writing_earlier_valid_entries(tmp_path, name, kind, target):
    source = archive_with(tmp_path, [("valid", tarfile.REGTYPE, ""), (name, kind, target)])
    with pytest.raises((TransferError, tarfile.FilterError)):
        unpack_directory(source, tmp_path / "destination")
    assert not (tmp_path / "destination/valid").exists()


def test_duplicate_archive_entries_are_not_applied(tmp_path):
    source = archive_with(tmp_path, [("valid", tarfile.REGTYPE, ""), ("valid", tarfile.REGTYPE, "")])
    with pytest.raises(TransferError, match="repeats"):
        unpack_directory(source, tmp_path / "destination")


def test_internal_relative_links_are_preserved(tmp_path):
    source = archive_with(tmp_path, [("valid", tarfile.REGTYPE, ""), ("alias", tarfile.SYMTYPE, "valid")])
    unpack_directory(source, tmp_path / "destination")
    assert (tmp_path / "destination/alias").is_symlink()
    assert (tmp_path / "destination/alias").read_text() == "ok"


@pytest.mark.parametrize("entries", [
    [("valid", tarfile.REGTYPE, ""), ("./valid", tarfile.REGTYPE, "")],
    [("valid", tarfile.REGTYPE, ""), ("alias", tarfile.SYMTYPE, "inside"), ("alias/file", tarfile.REGTYPE, "")],
    [("valid", tarfile.REGTYPE, ""), ("alias", tarfile.SYMTYPE, "second"), ("second", tarfile.SYMTYPE, "valid")],
])
def test_link_chains_and_normalized_duplicates_fail_before_extracting_contents(tmp_path, entries):
    source = archive_with(tmp_path, entries)
    with pytest.raises(TransferError):
        unpack_directory(source, tmp_path / "destination")
    assert not (tmp_path / "destination/valid").exists()
