"""The original evaluator owns resolution and each grading call gets fresh evidence."""

import os

import pytest
from evalhive_swebench.evaluation import grade

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_SWEBENCH_CATALOG") != "1",
    reason="Requires the verified SWE-bench 5.0.2 evaluator",
)


def spec():
    from swebench.types import TestSpec

    return TestSpec(
        instance_id="fixture__repo-1",
        image="fixture/image:unused",
        eval_script_list=[],
        repo="fixture/repo",
        version="fixture",
        FAIL_TO_PASS=["tests/test_fixture.py::test_fix"],
        PASS_TO_PASS=[],
        log_parser="parse_log_pytest",
        eval_type="pass_and_fail",
    )


def test_changed_patch_and_test_evidence_are_regraded_with_fresh_native_run_ids(tmp_path):
    path = tmp_path / "test.log"
    paths = tmp_path / "records"
    results = []
    for status in ("PASSED", "FAILED"):
        path.write_text(
            ">>>>> Applied Patch\n>>>>> Start Test Output\n"
            + status
            + " tests/test_fixture.py::test_fix\n>>>>> End Test Output\n>>>>> Test Exit Code: "
            + ("0" if status == "PASSED" else "1")
            + "\n"
        )
        results.append(grade(spec(), "Engineering patch " + status, path, paths))
    assert results[0]["report"]["fixture__repo-1"]["resolved"] is True
    assert results[1]["report"]["fixture__repo-1"]["resolved"] is False
    assert results[0]["metadata"]["run_id"] != results[1]["metadata"]["run_id"]
    assert results[0]["metadata"]["test_log_sha256"] != results[1]["metadata"]["test_log_sha256"]
    assert results[0]["metadata"]["patch_sha256"] != results[1]["metadata"]["patch_sha256"]
    assert len(list(paths.glob("*/report.json"))) == 2
    assert all(not result["metadata"]["cached_report_used"] for result in results)


@pytest.mark.parametrize("patch", [None, "", "Invalid patch"])
def test_missing_empty_and_rejected_patches_cannot_resolve_without_test_evidence(tmp_path, patch):
    path = tmp_path / "test.log"
    path.write_text(">>>>> Patch Apply Failed\n")
    result = grade(spec(), patch, path, tmp_path / "records")
    assert result["report"]["fixture__repo-1"]["resolved"] is False
    assert result["report"]["fixture__repo-1"]["patch_successfully_applied"] is False
