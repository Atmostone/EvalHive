"""Polling the native control socket must not initialize the SDK or its dependencies."""

import base64
import json
import subprocess
import sys
import threading
from pathlib import Path

from evalhive_benchmark import world_wire
from evalhive_benchmark.world_control import serve


def test_standalone_control_uses_only_the_standard_library(tmp_path):
    tmp_path.chmod(0o700)
    path, stop = tmp_path / "world.sock", threading.Event()
    thread = threading.Thread(
        target=serve,
        args=(lambda effect: lambda method, payload: {"ready": True}, path),
        kwargs={"stop": stop},
    )
    thread.start()
    request = {
        "id": "prepare",
        "scope": {"workspace_id": "workspace", "trial_id": "trial", "generation": 1},
        "operation": "begin",
        "payload": {"method": "prepare", "params": {}},
    }
    script = """import base64,json,runpy,sys
module=runpy.run_path(sys.argv[1],run_name='native_wire')
assert not any(name=='evalhive_benchmark' or name.startswith('jsonschema') for name in sys.modules)
request=json.loads(base64.b64decode(sys.argv[3]))
print(json.dumps(module['exchange'](request,sys.argv[2])))
"""
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                script,
                str(Path(world_wire.__file__)),
                str(path),
                base64.b64encode(json.dumps(request).encode()).decode(),
            ],
            capture_output=True,
            check=True,
            timeout=5,
        )
        assert json.loads(result.stdout) == {"operation_id": "prepare"}
    finally:
        stop.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
