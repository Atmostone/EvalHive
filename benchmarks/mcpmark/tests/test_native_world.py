"""Persistent private state uses the original lifecycle and verifier results."""

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from evalhive_mcpmark.assets import filesystem_asset
from evalhive_mcpmark.dataset import InstalledMCPMark, build_receipt
from evalhive_mcpmark.world_service import NativeService
from evalhive_mcpmark.world_stage import execute


@pytest.fixture
def native_world(tmp_path):
    initial = os.environ.get("EVALHIVE_MCPMARK_FILESYSTEM_DATA")
    code = os.environ.get("EVALHIVE_MCPMARK_NATIVE_ROOT")
    if not initial or not code:
        pytest.skip("Requires the pinned native wheel and original filesystem CDN input")
    asset = filesystem_asset(initial, "file_context")
    receipt = build_receipt(
        code,
        "verified",
        "sha256:" + "a" * 64,
        service="filesystem",
        suite="easy",
        assets=[asset],
    )
    installed = InstalledMCPMark(code, receipt)
    service = NativeService(
        installed,
        initial_root=initial,
        connection_root=tmp_path / "connection",
        records_root=tmp_path / "records",
    )
    case = next(case for case in receipt["cases"] if case["native_ref"]["task_id"] == "uppercase")
    try:
        yield service, case, asset
    finally:
        assert service("cleanup", {}) == {"cleaned": True}


def test_native_world_checks_frozen_initial_state_and_runs_verifier_after_agent_failure(
    native_world,
):
    service, case, initial = native_world
    prepared = service("prepare", {"case": case})
    assert prepared["initial_asset_digest"] == initial["source_digest"]
    assert prepared["prompt"] == service.installed.manager.get_task_instruction(service.task)
    connection = json.loads((service.connection_root / "connection.json").read_text())
    assert connection == {
        "service": "filesystem",
        "config": service.state.get_service_config_for_agent(),
    }
    working = Path(connection["config"]["test_directory"])
    assert working.exists() and not (working / "verify.py").exists()
    capture(service, success=False)
    result = service("verify", {})
    assert result["native_verdict_complete"] is True
    assert result["verifier_exit_code"] != 0 and result["native_result"]["success"] is False
    assert result == service("verify", {})
    assert filesystem_asset(service.initial_root, "file_context") == initial
    assert service("cleanup", {}) == {"cleaned": True} and not working.exists()


def test_native_world_reports_checker_timeout_as_incomplete_grading(native_world, monkeypatch):
    service, case, _ = native_world
    service("prepare", {"case": case})
    capture(service, success=True)

    def timed_out(task):
        raise subprocess.TimeoutExpired(["private-checker"], 300)

    monkeypatch.setattr(service.installed.manager, "run_verification", timed_out)
    result = service("verify", {})
    assert result["native_result"]["success"] is False
    assert result["native_verdict_complete"] is False and result["verifier_exit_code"] is None


def capture(service, *, success):
    root = service.records_root / "transcript"
    execute("write", {"offset": 0, "data": base64.b64encode(b"[]").decode()}, root=root)
    digest = hashlib.sha256(b"[]").hexdigest()
    execute("seal", {"size_bytes": 2, "sha256": digest}, root=root)
    service("capture", {"messages_sha256": digest, "agent_success": success})
