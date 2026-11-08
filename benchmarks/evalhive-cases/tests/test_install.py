"""Offline installation publishes immutable native and coordinator metadata together."""

import json

import pytest
from evalhive_benchmark import ProtocolError
from evalhive_benchmark.installation import dataset_key
from evalhive_cases.dataset import InstalledCases
from evalhive_cases.format import CaseFormatError
from evalhive_cases.install import install
from test_worker import IMAGE, installation


def test_snapshot_preserves_original_bytes_and_does_not_follow_later_source_changes(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    dataset = installation(source)
    output = tmp_path / "installed"
    receipt = install(source, output, suite="example", revision="rev-one", worker_image=IMAGE)
    assert receipt == dataset.receipt
    assert (output / "native/example/case.json").read_bytes() == (
        source / "example/case.json"
    ).read_bytes()
    installed = InstalledCases(output / "native")
    (source / "example/case.json").write_text("{}")
    assert installed.resolve(receipt["cases"][0]).id == "case-1"
    package = output / "registration/evalhive-cases"
    assert json.loads((package / "manifest.json").read_text())["image"] == IMAGE
    assert (
        json.loads(
            (package / "datasets" / (dataset_key("example", "rev-one") + ".json")).read_text()
        )
        == receipt
    )
    with pytest.raises(CaseFormatError):
        install(source, output, suite="example", revision="rev-one", worker_image=IMAGE)


def test_invalid_image_or_source_does_not_publish_a_partial_installation(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    installation(source)
    output = tmp_path / "invalid"
    with pytest.raises(ProtocolError):
        install(source, output, suite="example", revision="rev-one", worker_image="floating:latest")
    assert not output.exists()
    (source / "example/case.json").write_text("{}")
    with pytest.raises(CaseFormatError):
        install(source, output, suite="example", revision="rev-one", worker_image=IMAGE)
    assert not output.exists()
