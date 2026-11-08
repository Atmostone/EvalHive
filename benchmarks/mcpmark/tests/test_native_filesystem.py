"""The original filesystem manager and checker operate on the pinned CDN input."""

import hashlib
import importlib
import os
from pathlib import Path

import pytest
from evalhive_mcpmark.native import catalog


def proof(root):
    return [
        (
            path.relative_to(root).as_posix(),
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def test_original_filesystem_setup_verification_and_cleanup_leave_initial_data_unchanged(
    monkeypatch,
):
    data = os.environ.get("EVALHIVE_MCPMARK_FILESYSTEM_DATA")
    code = os.environ.get("EVALHIVE_MCPMARK_NATIVE_ROOT")
    if not data or not code:
        pytest.skip("Requires the pinned native wheel and original filesystem CDN input")
    manager, tasks = catalog(code, "filesystem", "easy")
    task = next(
        task for task in tasks if task.category_id == "file_context" and task.task_id == "uppercase"
    )
    initial = Path(data)
    before = proof(initial)
    monkeypatch.setenv("FILESYSTEM_TEST_ROOT", str(initial))
    native = importlib.import_module("src.mcp_services.filesystem.filesystem_state_manager")
    state = native.FilesystemStateManager(test_root=initial)
    try:
        assert state.set_up(task)
        working = Path(task.test_directory)
        assert working.is_dir() and not (working / "verify.py").exists()
        assert state.get_service_config_for_agent() == {"test_directory": str(working)}
        unsuccessful = manager.execute_task(task, {"success": True})
        assert unsuccessful.success is False and unsuccessful.verification_output
        # This deterministic fixture supplies the checker's known file effect;
        # it is not a benchmark execution or an alternate verifier.
        target = working / "uppercase"
        target.mkdir()
        for index in range(1, 6):
            name = f"file_{index:02d}.txt"
            (target / name).write_text((working / name).read_text().upper())
        (target / "answer.txt").write_text(
            "\n".join(
                f"file_{index:02d}.txt:"
                + str(len((working / f"file_{index:02d}.txt").read_text().split()))
                for index in range(1, 11)
            )
            + "\n"
        )
        successful = manager.execute_task(task, {"success": True})
        assert successful.success is True, successful.verification_error
        assert manager.run_verification(task).returncode == 0
        assert proof(initial) == before
    finally:
        assert state.clean_up(task)
    assert not working.exists() and proof(initial) == before
