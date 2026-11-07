"""Native service catalogs preserve metadata, private checks and exact variants."""

import os
from copy import deepcopy
from pathlib import Path

import pytest
from evalhive_mcpmark.dataset import InstalledMCPMark, build_receipt
from evalhive_mcpmark.native import NativeConfigurationError, catalog, inventory, native_code

IMAGE = "sha256:" + "a" * 64
COUNTS = {
    "filesystem": (30, 10),
    "postgres": (21, 10),
    "github": (23, 10),
    "notion": (28, 10),
    "playwright": (4, 0),
    "playwright_webarena": (21, 10),
}


@pytest.fixture
def source():
    root = os.getenv("EVALHIVE_MCPMARK_NATIVE_ROOT")
    if not root:
        pytest.skip("Requires the pinned native MCPMark source and isolated service dependencies")
    return Path(root)


def test_native_import_never_discovers_owner_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.setenv("EVALHIVE_MCPMARK_NATIVE_ROOT", str(tmp_path))
    with pytest.raises(NativeConfigurationError, match="disabled dotenv"):
        native_code()


def test_native_dataset_links_are_rejected_before_following_them(tmp_path):
    selected = tmp_path / "tasks/filesystem/standard/example/task"
    selected.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("private fixture")
    (selected / "verify.py").symlink_to(outside)
    with pytest.raises(NativeConfigurationError, match="links"):
        inventory(tmp_path, "filesystem", "standard")


@pytest.mark.parametrize("service", list(COUNTS))
@pytest.mark.parametrize("suite,index", [("standard", 0), ("easy", 1)])
def test_native_catalogs_and_frozen_receipts_use_actual_variant_membership(
    source, service, suite, index
):
    expected = COUNTS[service][index]
    if not expected:
        with pytest.raises(NativeConfigurationError, match="absent|no bounded"):
            build_receipt(source, "verified", IMAGE, service=service, suite=suite)
        return
    receipt = build_receipt(source, "verified", IMAGE, service=service, suite=suite)
    assert len(receipt["cases"]) == expected
    manager, tasks = catalog(source, service, suite)
    assert [case["title"] for case in receipt["cases"]] == [task.name for task in tasks]
    installed = InstalledMCPMark(source, receipt)
    selected = receipt["cases"][0]
    native = installed.resolve(selected)
    assert native.name == selected["title"]
    assert native.task_instruction_path.read_text() == tasks[0].task_instruction_path.read_text()
    assert native.get_task_instruction()
    if service != "playwright_webarena":
        assert manager.get_task_instruction(native)
    assert set(selected) == {"ref", "title", "source_digest", "native_ref", "requirements"}
    assert "verification_output" not in str(receipt) and "reference_answer" not in str(receipt)
    native.task_id = "local-only"
    assert installed.resolve(selected).name == selected["title"]
    changed = deepcopy(selected)
    changed["native_ref"]["suite"] = "foreign"
    with pytest.raises(NativeConfigurationError, match="differs"):
        installed.resolve(changed)


def test_native_code_changes_are_rejected_before_an_unpinned_module_can_run(
    source, tmp_path, monkeypatch
):
    import shutil

    shutil.copytree(source / "src", tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__"))
    target = tmp_path / "src/base/task_manager.py"
    target.write_text(target.read_text() + "\nraise RuntimeError('Must not execute')\n")
    monkeypatch.setenv("EVALHIVE_MCPMARK_NATIVE_ROOT", str(tmp_path))
    with pytest.raises(NativeConfigurationError, match="pinned revision"):
        native_code()
