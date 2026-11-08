"""Opt-in engineering fixtures for the pinned native filesystem toolchain."""

import json
import os
import subprocess

import pytest
from evalhive_harbor.staging import operate
from evalhive_harbor.transfers import BrokerTransfers


@pytest.fixture
def native_transfers():
    if os.environ.get("EVALHIVE_NATIVE_TRANSFERS") != "1":
        pytest.skip("Requires the pinned read-only filesystem toolchain")

    async def task_exec(argv, *, user, environment):
        assert user == "0" and environment == {"LD_PRELOAD": "", "LD_LIBRARY_PATH": ""}
        result = subprocess.run(argv, capture_output=True, text=True, env={**os.environ, **environment}, timeout=30)
        return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}

    async def stage_exec(argv):
        assert argv[:3] == ["/opt/venv/bin/python", "-m", "evalhive_harbor.staging"]
        result = operate("/evalhive-stage", argv[3], json.loads(argv[4]))
        return {"exit_code": 0, "stdout": json.dumps(result), "stderr": ""}

    return BrokerTransfers(task_exec, stage_exec)

