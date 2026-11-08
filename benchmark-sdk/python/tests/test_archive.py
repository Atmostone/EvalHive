"""Native archives retain unknowns and require explicit installed aggregators."""

from copy import deepcopy

import pytest
from evalhive_benchmark import ProtocolError
from evalhive_benchmark.archive import (
    canonical,
    create_archive,
    load_archive,
    recompute_archive,
    verify_archive,
)
from test_aggregation import receipt


def test_archive_round_trip_and_native_recompute_without_runtime(tmp_path):
    record = receipt()
    archive = create_archive(
        {"native_aggregates": [record], "usage": {"cost": None}}, include_native_inputs=True
    )
    file = tmp_path / "archive.json"
    file.write_bytes(canonical(archive))
    restored = load_archive(file)
    assert verify_archive(restored)["native_recompute_inputs"]
    key = ("fixture", "1", "sha256:" + "a" * 64, "source")
    result = recompute_archive(restored, {key: lambda payload: deepcopy(record["result"])})
    assert result == [
        {
            "status": "recomputed",
            "evaluation_id": "evaluation",
            "result_digest": record["result_digest"],
        }
    ]
    assert restored["sections"]["usage"]["cost"] is None


def test_public_archive_excludes_native_payload_and_cannot_claim_recompute():
    archive = create_archive({"native_aggregates": [receipt()]})
    assert b"retained tracker" not in canonical(archive) and b"native diagnostic" not in canonical(
        archive
    )
    assert not verify_archive(archive)["native_recompute_inputs"]
    with pytest.raises(ProtocolError, match="omits"):
        recompute_archive(archive, {})


def test_wrong_or_missing_aggregator_never_falls_back_to_saved_number():
    archive = create_archive({"native_aggregates": [receipt()]}, include_native_inputs=True)
    with pytest.raises(ProtocolError, match="Install"):
        recompute_archive(archive, {})
    with pytest.raises(ProtocolError, match="Install"):
        recompute_archive(
            archive, {("fixture", "other-version", "image", "source"): lambda payload: None}
        )


@pytest.mark.parametrize("field", ["sections", "policy", "section_digests", "manifest_digest"])
def test_altered_archive_sections_are_detected(field):
    archive = create_archive({"native_aggregates": [receipt()]}, include_native_inputs=True)
    archive[field] = {} if isinstance(archive[field], dict) else "changed"
    with pytest.raises(ProtocolError):
        verify_archive(archive)


def test_duplicate_keys_and_oversized_files_are_rejected(tmp_path, monkeypatch):
    from evalhive_benchmark import archive

    file = tmp_path / "archive.json"
    file.write_text('{"kind": "one", "kind": "two"}')
    with pytest.raises(ProtocolError):
        load_archive(file)
    monkeypatch.setattr(archive, "MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(ProtocolError, match="limit"):
        load_archive(file)
