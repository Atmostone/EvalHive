"""Runtime publication retains immutable images and relative private file grants."""

import json
import shutil

import pytest
from evalhive_benchmark import validate
from evalhive_toolathlon.commands import NativeConfigurationError
from evalhive_toolathlon.dataset import install
from evalhive_toolathlon.registration import register
from evalhive_toolathlon.source import file_digest
from test_source import source


def test_runtime_registration_publishes_pinned_data_without_host_path_authority(
    tmp_path, monkeypatch
):
    archive = source(tmp_path)

    def export(repository, destination):
        shutil.copyfile(archive, destination)
        return file_digest(destination)

    monkeypatch.setattr("evalhive_toolathlon.dataset.export_source", export)
    root = tmp_path / "installed"
    worker = "fixture-only/worker@sha256:" + "1" * 64
    install(tmp_path, root, revision="fixture-1", worker_image=worker)
    with pytest.raises(NativeConfigurationError):
        register(root, environment_image="floating:latest", postgres_image="sha256:" + "3" * 64)
    assert not (root / "manifest.json").exists()
    manifest = register(
        root,
        environment_image="sha256:" + "2" * 64,
        postgres_image="sha256:" + "3" * 64,
        egress=False,
    )
    validate("manifest", manifest)
    assert json.loads((root / "manifest.json").read_text())["image"] == worker
    files = json.loads((root / "files.json").read_text())["files"]
    assert (
        files["cases"]["path"] == "cases" and files["native-source"]["path"] == "native-source.tar"
    )
    assert files["harness-runtime"] == {"kind": "runtime_binding", "name": "harness.runtime"}
    resources = json.loads((root / "resources.json").read_text())["assets"]
    assert resources["toolathlon-gym.network"]["egress"] is False
    assert [value["file_id"] for value in resources["toolathlon-gym.task"]["mounts"]] == [
        "harness-runtime"
    ]
    assert not (root / ".manifest.pending.json").exists()
    with pytest.raises(NativeConfigurationError):
        register(root, environment_image="sha256:" + "2" * 64, postgres_image="sha256:" + "3" * 64)
