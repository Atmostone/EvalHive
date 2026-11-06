"""Pinned native source metadata is indexed without extracting executable input."""

import io
import json
import os
import shutil
import tarfile
from pathlib import Path

import pytest
from evalhive_cases.format import parse_case
from evalhive_toolathlon.commands import NativeConfigurationError, native_command
from evalhive_toolathlon.dataset import InstalledToolathlon, install
from evalhive_toolathlon.source import SourceArchive, file_digest


def source(tmp_path, *, member_override=None):
    target = tmp_path / "source.tar"
    documents = {
        "tasks/finalpool/native-case/task_config.json": json.dumps(
            {"needed_mcp_servers": ["terminal"]}
        ),
        "tasks/finalpool/native-case/docs/task.md": "Complete the public fixture task.",
        "tasks/finalpool/native-case/preprocess/main.py": "# native preparation fixture",
        "tasks/finalpool/native-case/evaluation/main.py": "# native checker fixture",
        "tasks/finalpool/native-case/groundtruth_workspace": None,
        "tasks/finalpool/native-case/groundtruth_workspace/answer.txt": "private-native-gold",
        "configs/mcp_servers/terminal.yaml": "name: terminal\nparams:\n  command: native-terminal\n",
    }
    with tarfile.open(target, "w") as archive:
        for name, value in documents.items():
            member = tarfile.TarInfo(name)
            if value is None:
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            else:
                data = value.encode()
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        if member_override:
            archive.addfile(member_override)
    return target


def test_native_case_gold_paths_and_source_digest_remain_private(tmp_path):
    path = source(tmp_path)
    archive = SourceArchive(path, file_digest(path))
    assert archive.case_ids() == ["native-case"]
    document = archive.case("native-case")
    case = parse_case(json.dumps(document).encode(), suffix=".json")
    assert case.agent_input()["prompt"] == "Complete the public fixture task."
    assert "private-native-gold" not in json.dumps(case.native_data())
    assert (
        case.external_eval["groundtruth_path"]
        == "tasks/finalpool/native-case/groundtruth_workspace"
    )
    assert case.native_data()["meta"]["native_archive_digest"] == archive.digest
    resources = archive.mcp_resources(["terminal"])
    resources[0]["config"]["command"] = "changed"
    assert archive.mcp_resources(["terminal"])[0]["config"]["command"] == "native-terminal"
    assert not (tmp_path / "tasks").exists()


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("hard-link", tarfile.LNKTYPE),
    ],
)
def test_native_archive_rejects_links_and_escaping_names_before_reading_metadata(
    tmp_path, name, kind
):
    member = tarfile.TarInfo(name)
    member.type, member.linkname = kind, "outside"
    path = source(tmp_path, member_override=member)
    with pytest.raises(NativeConfigurationError):
        SourceArchive(path, file_digest(path))


def test_tampered_native_source_is_rejected_before_case_resolution(tmp_path):
    path = source(tmp_path)
    digest = file_digest(path)
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(NativeConfigurationError, match="digest"):
        SourceArchive(path, digest)


def test_offline_dataset_freezes_source_and_case_receipts_without_a_runnable_image_claim(
    tmp_path, monkeypatch
):
    path = source(tmp_path)

    def export(repository, destination):
        shutil.copyfile(path, destination)
        return file_digest(destination)

    monkeypatch.setattr("evalhive_toolathlon.dataset.export_source", export)
    output = tmp_path / "installed"
    receipt = install(
        tmp_path,
        output,
        revision="fixture-1",
        worker_image="fixture-only/metadata@sha256:" + "1" * 64,
    )
    assert len(receipt["cases"]) == 1
    installed = InstalledToolathlon(output)
    case = installed.resolve(receipt["cases"][0])
    assert case.id == "native-case" and installed.archive.digest == file_digest(path)
    assert not (output / "tasks").exists()
    with pytest.raises(NativeConfigurationError):
        install(tmp_path, output, revision="fixture-1", worker_image=receipt["worker_image"])


def test_full_pinned_native_dataset_and_mcp_requirements_are_readable_without_host_extraction():
    if "TOOLATHLON_SOURCE_ARCHIVE" not in os.environ:
        pytest.skip("Set TOOLATHLON_SOURCE_ARCHIVE to verify the full pinned native source")
    path = Path(os.environ["TOOLATHLON_SOURCE_ARCHIVE"])
    archive = SourceArchive(path, os.environ["TOOLATHLON_ARCHIVE_DIGEST"])
    assert len(archive.case_ids()) == 503
    for identity in archive.case_ids():
        document = archive.case(identity)
        case = parse_case(json.dumps(document).encode(), suffix=".json")
        assert case.id == identity and case.agent_input()["prompt"]
        assert len(archive.mcp_resources(document["environment"]["mcp_servers"])) == len(
            document["environment"]["mcp_servers"]
        )
        for phase, key in (("preprocess", "preprocess_command"), ("evaluation", "eval_command")):
            assert (
                native_command(
                    case.external_eval[key],
                    case_id=identity,
                    phase=phase,
                    launch_time="2026-11-06 10:15:30 Friday",
                    groundtruth_path=case.external_eval.get("groundtruth_path"),
                )[0]
                == "/opt/venv/bin/python"
            )
