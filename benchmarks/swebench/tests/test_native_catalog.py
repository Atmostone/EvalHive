"""Original catalog loading preserves private assets and immutable membership."""

import json
import os
from pathlib import Path

import pytest
from evalhive_swebench.dataset import InstalledSWEbench, build_receipt, native_cases
from evalhive_swebench.native import SUBSETS, NativeConfigurationError

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_SWEBENCH_CATALOG") != "1",
    reason="Requires the verified SWE-bench 5.0.2 package",
)


@pytest.fixture
def source(tmp_path):
    (tmp_path / "sweb.yaml").write_text(
        json.dumps({"datasets": [SUBSETS["verified"]], "splits": ["test"]})
    )
    task = tmp_path / "tasks/fixture__repo-1"
    task.mkdir(parents=True)
    (task / "task.yaml").write_text(
        json.dumps(
            {
                "instance_id": "fixture__repo-1",
                "repo": "fixture/repo",
                "base_commit": "a" * 40,
                "image": "fixture/image:unresolved",
                "datasets": [SUBSETS["verified"]],
                "split": "test",
                "version": "1.0",
                "log_parser": "parse_log_pytest",
                "eval_type": "pass_and_fail",
            }
        )
    )
    for name, text in {
        "gold.patch": "Private reference patch",
        "test.patch": "Private test patch",
        "eval.sh": "#!/bin/bash\nexit 0\n",
        "problem_statement.md": "Fix the public issue",
    }.items():
        (task / name).write_text(text)
    (task / "tests.json").write_text(
        json.dumps({"FAIL_TO_PASS": ["test_fixture"], "PASS_TO_PASS": []})
    )
    return tmp_path, task


def test_native_discovery_keeps_private_assets_out_of_agent_descriptors(source):
    root, task = source
    receipt = build_receipt(
        root, "engineering", "sha256:" + "a" * 64, dataset_id="swebench-engineering-fixture"
    )
    assert len(receipt["cases"]) == 1
    assert receipt["cases"][0]["description"] == "Fix the public issue"
    assert "Private reference patch" not in json.dumps(receipt)
    assert "Private test patch" not in json.dumps(receipt)
    installed = InstalledSWEbench(root, receipt)
    native = installed.resolve(receipt["cases"][0])
    assert native["patch"] == "Private reference patch"
    (task / "test.patch").write_text("Changed evaluator tests")
    with pytest.raises(NativeConfigurationError, match="changed after installation"):
        installed.resolve(receipt["cases"][0])


def test_unpublished_splits_and_unknown_subsets_do_not_silently_shrink_a_catalog(source):
    root, task = source
    with pytest.raises(NativeConfigurationError, match="does not publish"):
        native_cases(root, "verified", "dev")
    with pytest.raises(NativeConfigurationError, match="must be explicit"):
        native_cases(root, "multilingual", "test")


def test_links_are_rejected_before_native_discovery_reads_protected_contents(source):
    root, task = source
    (task / "gold.patch").unlink()
    (task / "gold.patch").symlink_to(Path("/uninstalled/evaluator-input"))
    with pytest.raises(NativeConfigurationError, match="links"):
        native_cases(root, "verified", "test")


def test_image_installation_preserves_membership_and_changes_frozen_case_identity(source):
    root, _ = source
    worker = "sha256:" + "a" * 64
    missing = build_receipt(root, "fixture", worker)
    installed = build_receipt(
        root, "fixture", worker, installed_images={"fixture__repo-1": "sha256:" + "b" * 64}
    )
    assert missing["cases"][0]["metadata"]["execution_availability"] == "unavailable"
    assert installed["cases"][0]["metadata"]["execution_availability"] == "installed"
    assert missing["cases"][0]["ref"] == installed["cases"][0]["ref"]
    assert missing["cases"][0]["source_digest"] != installed["cases"][0]["source_digest"]
    assert missing["source_digest"] == installed["source_digest"]
    assert (
        InstalledSWEbench(root, installed).resolve(installed["cases"][0])["instance_id"]
        == "fixture__repo-1"
    )
    with pytest.raises(NativeConfigurationError, match="immutable"):
        build_receipt(
            root, "fixture", worker, installed_images={"fixture__repo-1": "unresolved:latest"}
        )
    with pytest.raises(NativeConfigurationError, match="unknown native case"):
        build_receipt(root, "fixture", worker, installed_images={"another__repo-1": worker})
