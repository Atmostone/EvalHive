"""Evidence transfers reject incomplete writes and unsafe retained archives."""

import base64
import hashlib
import io
import json
import tarfile

import pytest
from evalhive_tau2 import stage


def test_private_evidence_is_sealed_before_use_and_chunk_order_is_strict(tmp_path, monkeypatch):
    monkeypatch.setattr(stage, "ROOT", tmp_path / "records")
    assert stage.execute("init", {})["initialized"]
    data = b'{"private":"native-simulation"}'
    payload = {"name": "checkpoint", "offset": 0, "data": base64.b64encode(data).decode()}
    assert stage.execute("write", payload)["size_bytes"] == len(data)
    with pytest.raises(ValueError, match="order"):
        stage.execute("write", payload)
    with pytest.raises(ValueError, match="incomplete"):
        stage.execute("seal", {"name": "checkpoint", "size_bytes": len(data), "sha256": "a" * 64})
    assert stage.execute(
        "seal",
        {"name": "checkpoint", "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
    )["sealed"]
    result = stage.execute("read", {"name": "checkpoint", "offset": 0})
    assert json.loads(base64.b64decode(result["data"])) == {"private": "native-simulation"}
    assert (stage.ROOT / "checkpoint" / "record.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="name"):
        stage.execute("read", {"name": "../foreign", "offset": 0})


@pytest.mark.parametrize(
    "name,kind",
    [("../record.json", "file"), ("record.json", "symlink"), ("record.json", "hardlink")],
)
def test_retained_archive_never_extracts_links_or_foreign_paths(tmp_path, monkeypatch, name, kind):
    archive = tmp_path / "payload"
    with tarfile.open(archive, "w") as stream:
        member = tarfile.TarInfo(name)
        if kind != "file":
            member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            member.linkname = "/foreign"
            stream.addfile(member)
        else:
            member.size = 2
            stream.addfile(member, io.BytesIO(b"{}"))
    monkeypatch.setattr(stage, "ROOT", tmp_path / "records")
    monkeypatch.setattr(stage, "RETAINED", tmp_path)
    stage.execute("init", {})
    (tmp_path / "owned").mkdir()
    archive.rename(tmp_path / "owned" / "payload")
    with pytest.raises(ValueError, match="member"):
        stage.execute("restore", {"artifact_id": "owned"})
