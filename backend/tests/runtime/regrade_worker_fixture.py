"""SDK-only native checkpoint fixture; restoration belongs to the connector."""

import asyncio
from copy import deepcopy

from evalhive_benchmark.connector import serve_stdio
from evalhive_benchmark.protocol import ProtocolError

if __package__:
    from .broker_worker_fixture import PROVIDER, Worker, broker
else:
    from broker_worker_fixture import PROVIDER, Worker, broker


class RegradeWorker(Worker):
    manifest = deepcopy(Worker.manifest)
    manifest["entrypoint"] = ["python", "/fixture/regrade_worker.py"]
    manifest["image"] = "evalhive-benchmark-regrade-fixture:b05-inputs"
    manifest["capabilities"]["regrade"] = "native"

    async def handle(self, method, params, context):
        if "spec" in params:
            self.spec = params["spec"]
            self.config = self.spec["config"]
        if method == "plan":
            plan = await super().handle(method, params, context)
            plan["outputs"].append(
                {
                    "id": "world",
                    "resource_id": "task",
                    "path": "/workspace",
                    "visibility": "private",
                    "required": True,
                }
            )
            if self.spec.get("purpose") == "evaluation":
                plan.pop("agent")
                plan["purpose"] = "evaluation"
                plan["resources"][0]["config"]["retained_inputs"] = ["world"]
            return plan
        if method == "prepare" and self.spec.get("purpose") == "evaluation":
            scope = self.spec["scope"]
            task = await broker(
                context,
                scope,
                PROVIDER,
                "allocate",
                {"resource_id": "task"},
                request_id="restore.allocate",
            )
            program = """import os,tarfile
from pathlib import Path
source=Path('/evalhive/retained/world/payload')
assert source.stat().st_size > 32*1024*1024
try:
 source.open('ab')
 raise AssertionError('Checkpoint mount must be read-only')
except OSError:
 pass
with tarfile.open(source) as archive:
 members=archive.getmembers()
 assert {member.name for member in members} == {'workspace','workspace/result.txt','workspace/padding.bin'}
 for member in members:
  assert member.isdir() or member.isfile()
  if member.isfile():
   target=Path('/workspace')/Path(member.name).name
   with archive.extractfile(member) as original,target.open('wb') as output:
    while chunk:=original.read(1024*1024): output.write(chunk)
assert Path('/workspace/padding.bin').stat().st_size == 33*1024*1024
print('native checkpoint restored')
"""
            restored = await broker(
                context,
                scope,
                PROVIDER,
                "exec",
                {
                    "argv": ["python", "-c", program],
                    "cwd": "/workspace",
                    "timeout_seconds": 20,
                },
                handle=task["handle"],
                request_id="restore.exec",
            )
            if restored["exit_code"] != 0:
                raise ProtocolError(
                    "RESTORE_FAILED", "Native fixture checkpoint restoration failed", "preparation"
                )
            return {
                "purpose": "evaluation",
                "ready": True,
                "session_handle": task["handle"],
                "native_state": {"restored": True},
                "artifacts": [],
            }
        if method == "drive":
            result = await super().handle(method, params, context)
            command = await broker(
                context,
                params["scope"],
                PROVIDER,
                "exec",
                {
                    "argv": [
                        "python",
                        "-c",
                        "from pathlib import Path;p=Path('/workspace/padding.bin');p.write_bytes(b'x'*(33*1024*1024))",
                    ],
                    "timeout_seconds": 10,
                },
                handle=params["payload"]["session_handle"],
                request_id="drive.padding",
            )
            if command["exit_code"] != 0:
                raise ProtocolError(
                    "FIXTURE_FAILED", "Native checkpoint fixture failed", "execution"
                )
            return result
        if method == "capture":
            result = await super().handle(method, params, context)
            world = await broker(
                context,
                params["scope"],
                PROVIDER,
                "export",
                {"output_id": "world"},
                handle=params["payload"]["session_handle"],
                request_id="capture.world",
            )
            result["artifacts"].append(world["artifact"])
            result["checkpoint"] = {
                "format_version": "1.0",
                "connector": {key: self.manifest[key] for key in ("id", "version")},
                "case": self.spec["case"]["ref"],
                "source_digest": self.spec["case"]["source_digest"],
                "state_schema": "fixture.world.v1",
                "complete": True,
                "artifacts": [world["artifact"]],
                "evaluator_roles": [],
                "state": {},
            }
            return result
        return await super().handle(method, params, context)


if __name__ == "__main__":
    asyncio.run(serve_stdio(RegradeWorker()))
