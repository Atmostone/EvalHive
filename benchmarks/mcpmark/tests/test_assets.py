"""Empty directories are part of the original filesystem initial state."""

from evalhive_mcpmark.assets import filesystem_asset


def test_initial_fingerprint_includes_empty_directories_and_their_permissions(tmp_path):
    category = tmp_path / "file_context"
    category.mkdir()
    (category / "input.txt").write_text("original input")
    original = filesystem_asset(tmp_path, "file_context")
    empty = category / "empty"
    empty.mkdir()
    added = filesystem_asset(tmp_path, "file_context")
    assert (
        original["source_digest"] != added["source_digest"] and original["files"] == added["files"]
    )
    empty.chmod(0o700)
    assert filesystem_asset(tmp_path, "file_context")["source_digest"] != added["source_digest"]
