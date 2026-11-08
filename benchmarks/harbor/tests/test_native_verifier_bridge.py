"""Run the official verifier with real shell execution and pinned file transfers."""

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from evalhive_harbor.artifacts import collect_artifacts, restore_artifacts
from evalhive_harbor.environment import BrokerEnvironment
from evalhive_harbor.native import parse_task
from evalhive_harbor.verification import run_verifier
from harbor.models.trial.paths import TrialPaths
from test_native import task


@pytest.mark.asyncio
async def test_official_verifier_uses_real_broker_commands_and_trusted_file_transfers(native_transfers, tmp_path):
    root = task(tmp_path, '[verifier]\ntimeout_sec=10\nenv={NATIVE_SCORE="0.625"}\n')
    (root / "tests/test.sh").write_text(
        '#!/bin/bash\nprintf \'{"quality":%s}\' "$NATIVE_SCORE" > /logs/verifier/reward.json\n'
        'printf "native checker output\\n"\nexit 17\n'
    )
    native = parse_task(root)
    Path("/logs/verifier").mkdir(parents=True, exist_ok=True)
    Path("/logs/verifier/reward.txt").write_text("999")
    calls = []

    async def broker(scope, provider, operation, payload, **options):
        assert scope == {"trial_id": "trial"} and provider == "registered.provider"
        assert operation == "exec" and options["handle"] == "owned-task"
        calls.append(payload)
        result = subprocess.run(
            payload["argv"], cwd=payload["cwd"],
            env={**os.environ, **payload.get("environment", {})},
            capture_output=True, text=True, timeout=payload["timeout_seconds"],
        )
        return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}

    environment = BrokerEnvironment(
        SimpleNamespace(broker=broker), {"trial_id": "trial"},
        "registered.provider", "owned-task", cwd=str(tmp_path),
        deadline=time.monotonic() + 15, transfers=native_transfers,
    )
    paths = TrialPaths(tmp_path / "recorded")
    paths.mkdir()
    result = await run_verifier(native, environment, paths)
    assert result.rewards == {"quality": 0.625}
    assert paths.reward_json_path.read_text() == '{"quality":0.625}'
    assert not paths.reward_text_path.exists()
    assert "native checker output" in paths.test_stdout_path.read_text()
    assert any(payload.get("environment") == {"NATIVE_SCORE": "0.625"} for payload in calls)
    assert environment.recording_complete
    Path("/logs/artifacts").mkdir(parents=True, exist_ok=True)
    Path("/logs/artifacts/answer.txt").write_text("native recorded artifact")
    collected = await collect_artifacts(native, environment, paths)
    assert collected["complete"]
    assert collected["manifest"][0]["status"] == "ok"
    Path("/logs/artifacts/answer.txt").unlink()
    await restore_artifacts(native, environment, paths)
    assert Path("/logs/artifacts/answer.txt").read_text() == "native recorded artifact"
