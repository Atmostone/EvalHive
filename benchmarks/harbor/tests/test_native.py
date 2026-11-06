"""Pinned native parsing preserves requirements and rejects discarded conditions."""

import os
from pathlib import Path

import pytest
from evalhive_harbor.native import HarborConfigurationError, parse_task, source_digest


def task(tmp_path, extra=""):
    root = tmp_path / "native-task"
    (root / "environment").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "environment/Dockerfile").write_text("FROM scratch\n")
    (root / "tests/test.sh").write_text("#!/bin/sh\nprintf '1' > /logs/verifier/reward.txt\n")
    (root / "instruction.md").write_text("<!-- canary fixture -->\n\nPublic instruction\n")
    (root / "task.toml").write_text('version = "1.0"\n' + extra)
    return root


def test_native_parser_owns_instruction_and_explicit_resource_requirements(tmp_path):
    root = task(tmp_path, "[environment]\ncpus = 1.5\nmemory_mb = 8192\nstorage_mb = 20000\n")
    parsed = parse_task(root)
    assert parsed.task.instruction == "Public instruction\n"
    assert parsed.verifier_mode == "shared"
    assert parsed.requirements()["resources"] == {
        "cpu_cores": 1.5,
        "memory_mb": 8192,
        "disk_mb": 20000,
    }
    assert parsed.raw_config["environment"]["memory_mb"] == 8192
    original = parsed.source_digest
    (root / "tests/test.sh").write_text("Changed private checker\n")
    assert source_digest(root) != original


@pytest.mark.parametrize(
    "extra",
    [
        "unknown_native_condition = true\n",
        "[environment]\nmemory_bm = 2048\n",
        "[verifier]\nunknown_grader_option = true\n",
    ],
)
def test_unknown_configuration_cannot_silently_disappear_in_native_pydantic(tmp_path, extra):
    with pytest.raises(HarborConfigurationError, match="unknown or unsupported"):
        parse_task(task(tmp_path, extra))


def test_native_separate_verifier_and_unknown_hardware_remain_explicit(tmp_path):
    root = task(
        tmp_path,
        '[environment]\ngpus = 1\nnetwork_mode = "allowlist"\n'
        'allowed_hosts = ["example.invalid"]\n[verifier]\nenvironment_mode = "separate"\n',
    )
    parsed = parse_task(root)
    assert parsed.verifier_mode == "separate"
    assert {"hardware.gpu", "network.allowlist"} <= set(parsed.requirements()["capabilities"])


def test_native_source_proof_rejects_links_special_files_and_oversize_bytes(tmp_path):
    root = task(tmp_path)
    (root / "external").symlink_to(tmp_path / "not-a-task-file")
    with pytest.raises(HarborConfigurationError, match="unsupported filesystem"):
        parse_task(root)
    (root / "external").unlink()
    os.mkfifo(root / "pipe")
    with pytest.raises(HarborConfigurationError, match="unsupported filesystem"):
        parse_task(root)
    (root / "pipe").unlink()
    with pytest.raises(HarborConfigurationError, match="byte limit"):
        source_digest(root, max_bytes=1)


@pytest.mark.skipif(
    not os.getenv("TERMINAL_BENCH_SOURCE"), reason="Pinned native corpus not installed"
)
def test_complete_terminal_bench_two_corpus_parses_with_native_parser():
    root = Path(os.environ["TERMINAL_BENCH_SOURCE"])
    paths = sorted(root.glob("*/task.toml"))
    assert len(paths) >= 80
    for path in paths:
        parsed = parse_task(path.parent)
        assert parsed.task.instruction and parsed.source_digest.startswith("sha256:")
