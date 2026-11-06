"""Standalone fixture package: imports the Benchmark SDK, never backend modules."""

import asyncio
import importlib.util
import json
import os
import sys

from evalhive_benchmark.connector import Connector, serve_stdio
from evalhive_benchmark.protocol import ProtocolError

PROVIDER = "fixture.rootless"


async def broker(context, *args, **kwargs):
    return await context.broker(
        *args, **kwargs, durable_host=os.getenv("EVALHIVE_DURABLE_BROKER") == "1"
    )


PLAN = {
    "resources": [
        {
            "id": "task",
            "kind": "container",
            "provider": PROVIDER,
            "config": {"asset_id": "fixture.task"},
        }
    ],
    "agent": {
        "mode": "in_environment",
        "interaction": "autonomous",
        "environment_handle": "task",
        "cwd": "/workspace",
        "prompt": "Fixture only",
        "instructions": [],
        "resources": [],
    },
    "outputs": [
        {
            "id": "result",
            "resource_id": "task",
            "path": "/workspace/result.txt",
            "visibility": "user",
            "required": True,
        }
    ],
    "requirements": {},
    "grading_mode": "environment",
}
CASE = {
    "ref": {"dataset_id": "provider-fixture", "revision": "1", "case_id": "write"},
    "title": "Rootless SDK resource fixture",
    "source_digest": "sha256:" + "1" * 64,
    "native_ref": {"case": "write"},
    "requirements": {},
}


class Worker(Connector):
    manifest = {
        "protocol_version": "1.0",
        "kind": "benchmark",
        "id": "fixture.benchmark",
        "version": "1.0",
        "display_name": "SDK resource fixture",
        "entrypoint": ["python", "/fixture/worker.py"],
        "image": "evalhive-broker-worker-fixture:development",
        "config_schema": {"type": "object"},
        "capabilities": {
            key: "native" for key in ["prepare", "drive", "capture", "evaluate", "cleanup"]
        },
        "requirements": {},
    }

    async def handle(self, method, params, context):
        if "spec" in params:
            self.config = params["spec"]["config"]
        if method == "resolve_case":
            return CASE
        if method == "list_cases":
            return {"cases": [CASE], "next_cursor": None}
        if method == "plan":
            return PLAN
        if method == "validate":
            return {"accepted": True, "effective_config": params["spec"]["config"]}
        if method == "prepare":
            scope = params["spec"]["scope"]
            task = await broker(
                context,
                scope,
                PROVIDER,
                "allocate",
                {"resource_id": "task"},
                request_id="prepare.allocate",
            )
            program = """import os,time
from pathlib import Path
assert Path(os.environ['A_CONFIG']).read_text()=='alpha-private-fixture'
assert Path(os.environ['Z_CONFIG']).read_text()=='zeta-private-fixture'
Path('result.txt').write_text('Prepared through the Benchmark SDK\\n')
child=os.fork()
if child==0:
 os.setsid();Path('/tmp/retained-service.pid').write_text(str(os.getpid()))
 while True: time.sleep(1)
while not Path('/tmp/retained-service.pid').exists(): time.sleep(.01)
print('x'*(2*1024*1024),end='',flush=True)
"""
            command = await broker(
                context,
                scope,
                PROVIDER,
                "exec",
                {
                    "argv": ["python", "-c", program],
                    "cwd": "/workspace",
                    "timeout_seconds": 10,
                    "private_input_refs": {"Z_CONFIG": "fixture.zeta", "A_CONFIG": "fixture.alpha"},
                },
                handle=task["handle"],
                request_id="prepare.exec",
            )
            if (
                command["exit_code"] != 0
                or not command["stdout_truncated"]
                or len(command["stdout"]) != 65536
            ):
                raise ProtocolError("FIXTURE_FAILED", "Native preparation failed", "preparation")
            assert not os.path.exists("/run/user/11001/docker.sock")
            assert not os.path.exists("/var/run/docker.sock")
            assert not any(
                name == "app" or name.startswith("app.") or name == "docker" for name in sys.modules
            )
            assert importlib.util.find_spec("app") is None
            assert importlib.util.find_spec("docker") is None
            return {
                "ready": True,
                "session_handle": task["handle"],
                "agent": {**PLAN["agent"], "environment_handle": task["handle"]},
                "native_state": {"exec_handle": command["handle"], "isolated": True},
                "artifacts": [],
            }
        if method == "drive":
            scope, task_handle = params["scope"], params["payload"]["session_handle"]
            await context.emit(
                scope, "agent.output", {"text": "Local execution fixture"}, source="agent"
            )
            program = "from pathlib import Path;p=Path('/workspace/result.txt');p.write_text(p.read_text()+'Driven through the Benchmark SDK\\n')"
            if getattr(self, "config", {}).get("wait_drive"):
                program = """import os,signal,time
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
if os.fork()==0:
 os.setsid()
 while True: time.sleep(1)
Path('/tmp/agent.started').write_text('ready')
while True: time.sleep(1)
"""
            executed = await broker(
                context,
                scope,
                PROVIDER,
                "exec",
                {
                    "argv": [
                        "python",
                        "-c",
                        program,
                    ],
                    "timeout_seconds": 30 if getattr(self, "config", {}).get("wait_drive") else 10,
                },
                handle=task_handle,
                request_id="drive.exec",
            )
            if executed["exit_code"] != 0:
                raise ProtocolError("FIXTURE_FAILED", "Native execution failed", "execution")
            await context.emit(
                scope, "phase.completed", {"phase": "driving", "status": "success"}, source="agent"
            )
            return {"finished": True, "exec_handle": executed["handle"]}
        if method == "evaluate":
            task_handle = params["payload"]["session_handle"]
            command = await broker(
                context,
                params["scope"],
                PROVIDER,
                "exec",
                {
                    "argv": [
                        "python",
                        "-c",
                        "import json;from pathlib import Path;print(json.dumps({'correct':Path('/workspace/result.txt').read_text()=='Prepared through the Benchmark SDK\\nDriven through the Benchmark SDK\\n'}))",
                    ],
                    "timeout_seconds": 10,
                },
                handle=task_handle,
                request_id="evaluate.exec",
            )
            correct = json.loads(command["stdout"])["correct"]
            return {
                "status": "completed",
                "success": correct,
                "metrics": [{"name": "fixture_reward", "value": int(correct)}],
                "primary_metric": "fixture_reward",
                "native": {"fixture": True},
                "artifacts": [],
                "completeness": {"state": "captured"},
            }
        if method == "capture":
            result = await broker(
                context,
                params["scope"],
                PROVIDER,
                "export",
                {"output_id": "result"},
                handle=params["payload"]["session_handle"],
                request_id="capture.result",
            )
            return {"artifacts": [result["artifact"]], "regrade": "available"}
        if method == "cleanup":
            return {"cleaned": True}  # The host releases its durable resource graph.
        if method == "cancel":
            return {"cancelled": True}
        raise ProtocolError(
            "UNSUPPORTED_CAPABILITY", "Fixture method is unavailable", "unsupported"
        )


if __name__ == "__main__":
    asyncio.run(serve_stdio(Worker()))
