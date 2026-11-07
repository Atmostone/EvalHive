"""Installation records preserve native conditions without exposing checker bytes."""

import os
from copy import deepcopy

import pytest
from evalhive_harbor.dataset import InstalledHarbor, build_receipt
from evalhive_harbor.native import HarborConfigurationError
from test_native import task

IMAGE = "sha256:" + "a" * 64


def test_native_receipt_is_deterministic_and_resolves_only_exact_source(tmp_path):
    root = task(tmp_path, "[environment]\ncpus = 1.5\nmemory_mb = 8192\nallow_internet = false\n")
    (root / "tests/private-gold.txt").write_text("Private verifier input")
    receipt = build_receipt(tmp_path, "native-revision", IMAGE)
    assert receipt == build_receipt(tmp_path, "native-revision", IMAGE)
    (case,) = receipt["cases"]
    assert case["ref"]["case_id"] == "native-task"
    assert case["requirements"]["resources"] == {"cpu_cores": 1.5, "memory_mb": 8192}
    assert "host.model_gateway_v1" in case["requirements"]["capabilities"]
    assert "Private verifier input" not in str(receipt)
    installed = InstalledHarbor(tmp_path, receipt)
    assert installed.resolve(case).task.instruction == "Public instruction\n"
    (root / "tests/private-gold.txt").write_text("Changed private verifier input")
    with pytest.raises(HarborConfigurationError, match="changed"):
        installed.resolve(case)


@pytest.mark.parametrize("change", ["revision", "requirement", "path", "source"])
def test_worker_cannot_substitute_a_native_case_or_its_applied_requirements(tmp_path, change):
    task(tmp_path)
    receipt = build_receipt(tmp_path, "frozen", IMAGE)
    installed = InstalledHarbor(tmp_path, receipt)
    selected = deepcopy(receipt["cases"][0])
    if change == "revision":
        selected["ref"]["revision"] = "another"
    elif change == "requirement":
        selected["requirements"]["resources"]["memory_mb"] = 1
    elif change == "path":
        selected["native_ref"]["path"] = "../private"
    else:
        selected["source_digest"] = "sha256:" + "b" * 64
    with pytest.raises(HarborConfigurationError, match="frozen"):
        installed.resolve(selected)


def test_nested_native_names_remain_opaque_identities_independent_of_path_resolution(tmp_path):
    parent = tmp_path / "nested"
    parent.mkdir()
    task(parent)
    receipt = build_receipt(tmp_path, "frozen", IMAGE, task_paths=["nested/native-task"])
    (case,) = receipt["cases"]
    assert case["title"] == "nested/native-task"
    assert case["ref"]["case_id"] == "nested/native-task"
    assert InstalledHarbor(tmp_path, receipt).resolve(case)
    with pytest.raises(HarborConfigurationError):
        build_receipt(tmp_path, "frozen", IMAGE, task_paths=["../private"])


def test_separate_native_verifier_requirements_preserve_its_own_budget(tmp_path):
    task(
        tmp_path,
        '[environment]\ncpus=4\nmemory_mb=8192\nstorage_mb=20000\n[verifier]\nenvironment_mode="separate"\n[verifier.environment]\ncpus=0.25\nmemory_mb=128\nstorage_mb=64\n',
    )
    receipt = build_receipt(tmp_path, "native-role-budgets", IMAGE)
    (case,) = receipt["cases"]
    assert case["requirements"]["resources"] == {
        "cpu_cores": 4,
        "memory_mb": 8192,
        "disk_mb": 20000,
    }
    assert case["evaluation_requirements"]["resources"] == {
        "cpu_cores": 0.25,
        "memory_mb": 128,
        "disk_mb": 64,
    }
    assert InstalledHarbor(tmp_path, receipt).resolve(case)


@pytest.mark.skipif(
    not os.getenv("TERMINAL_BENCH_SOURCE"), reason="Pinned native corpus not installed"
)
def test_complete_terminal_bench_two_catalog_conforms_to_sdk_installation_contract():
    receipt = build_receipt(os.environ["TERMINAL_BENCH_SOURCE"], "pinned-native-corpus", IMAGE)
    assert len(receipt["cases"]) == 89
    assert all(case["ref"]["case_id"] == case["native_ref"]["path"] for case in receipt["cases"])
    assert receipt["provenance"]["harbor_version"] == "0.25.0"
