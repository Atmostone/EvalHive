"""Native source inventories reject links and changing protected task inputs."""

import pytest
from evalhive_swebench.native import NativeConfigurationError, directory, file_record, inventory


def test_task_inventory_includes_private_checker_files_without_reading_them_into_descriptors(
    tmp_path,
):
    (tmp_path / "sweb.yaml").write_text("datasets: []\n")
    task = tmp_path / "tasks/project__repo-1"
    task.mkdir(parents=True)
    for name in ("task.yaml", "gold.patch", "test.patch", "eval.sh", "problem_statement.md"):
        (task / name).write_text("Engineering fixture " + name)
    records = inventory(tmp_path, ["project__repo-1"])
    assert len(records) == 6
    assert not any("Engineering fixture" in str(row) for row in records)
    before = next(row for row in records if row["path"].endswith("gold.patch"))
    (task / "gold.patch").write_text("Changed protected input")
    assert file_record(tmp_path, task / "gold.patch")["sha256"] != before["sha256"]


def test_inventory_rejects_symlinked_task_files_and_roots(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(NativeConfigurationError, match="without links"):
        directory(alias)
    (real / "sweb.yaml").write_text("datasets: []\n")
    task = real / "tasks/project__repo-1"
    task.mkdir(parents=True)
    (task / "gold.patch").symlink_to(real / "sweb.yaml")
    with pytest.raises(NativeConfigurationError, match="links"):
        inventory(real, ["project__repo-1"])
