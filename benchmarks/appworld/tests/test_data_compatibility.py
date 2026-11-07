"""Incompatible native data is rejected before any world or API is started."""

import pytest
from evalhive_appworld import native


@pytest.mark.parametrize("version_file", ["version.txt", "base_dbs/version.txt"])
def test_data_and_database_versions_must_match_the_pinned_release(
    tmp_path, monkeypatch, version_file
):
    (tmp_path / "data/base_dbs").mkdir(parents=True)
    for name in ("version.txt", "base_dbs/version.txt"):
        (tmp_path / "data" / name).write_text("0.1.0" if name == version_file else "0.2.0")
    monkeypatch.setenv("APPWORLD_ROOT", str(tmp_path))
    monkeypatch.setenv("APPWORLD_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(native, "verified_code", lambda: None)
    with pytest.raises(native.NativeConfigurationError, match="version is incompatible"):
        native.load(tmp_path)


def test_foreign_environment_root_is_rejected_before_native_import(tmp_path, monkeypatch):
    monkeypatch.setenv("APPWORLD_ROOT", str(tmp_path / "other"))
    monkeypatch.setenv("APPWORLD_CACHE", str(tmp_path / "cache"))
    with pytest.raises(native.NativeConfigurationError, match="explicit roots"):
        native.load(tmp_path)
