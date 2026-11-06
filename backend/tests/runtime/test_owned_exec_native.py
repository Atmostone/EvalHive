"""Opt-in rootless engineering checks; no external services or model requests.

Run on the execution host with EVALHIVE_NATIVE_RUNTIME=1. The artifact and task
images must already be built. Each test owns and removes its temporary container.
"""

import asyncio
import io
import json
import os
import tarfile
import time
import uuid
from dataclasses import replace

import docker
import pytest
from app.runtime.managed_docker import RootlessDockerRuntime
from app.runtime.managed_exec import OwnedDockerProcess, OwnedExecRuntime
from app.runtime.managed_spec import (
    ContainerSpec,
    ExecSpec,
    ManagedRuntimeError,
    OwnerScope,
    PathMapping,
    Quota,
    RuntimePolicy,
)
from app.runtime.spec import BindMount

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_RUNTIME") != "1",
    reason="Requires explicit rootless native runtime checks",
)


def identity():
    return str(uuid.uuid4())


@pytest.fixture
def environment(tmp_path):
    client = docker.DockerClient(base_url="unix:///run/user/11001/docker.sock")
    bundle, data = tmp_path / "bundle", tmp_path / "data"
    bundle.mkdir(mode=0o755)
    data.mkdir()
    source = client.containers.create(
        os.getenv("EVALHIVE_SUPERVISOR_IMAGE", "evalhive-supervisor:development"),
        entrypoint=["/evalhive-supervisor", "--capabilities"],
        labels={"evalhive.purpose": "owned-exec-native-export"},
    )
    try:
        chunks, _ = source.get_archive("/evalhive-supervisor")
        content = bytearray()
        for chunk in chunks:
            content.extend(chunk)
            assert len(content) < 2 * 1024 * 1024
        with tarfile.open(fileobj=io.BytesIO(content)) as archive:
            member = archive.getmember("evalhive-supervisor")
            assert member.isfile() and member.size < 1024 * 1024
            (bundle / "supervisor").write_bytes(archive.extractfile(member).read())
            (bundle / "supervisor").chmod(0o755)
    finally:
        source.remove(force=True, v=True)
    scope = OwnerScope(identity(), identity(), identity(), 1)
    runtime = RootlessDockerRuntime(
        client,
        RuntimePolicy(PathMapping(str(data), str(data)), (PathMapping(str(bundle), str(bundle)),)),
    )
    image = runtime.resolve_image("evalhive-environment-workspace:0.1.0")["image_id"]
    container = runtime.create(
        ContainerSpec(
            scope,
            identity(),
            image,
            command=("python", "-c", "import time;time.sleep(600)"),
            mounts=(BindMount(str(bundle), "/evalhive-runtime"),),
            quota=Quota(256, 1, 128),
        )
    )
    try:
        yield (
            runtime,
            OwnedExecRuntime(runtime),
            container,
            client.containers.get(container.container_id),
        )
    finally:
        runtime.stop(container, grace_seconds=0, remove=True)
        client.close()


async def start(environment, program, **options):
    _, runtime, container, _ = environment
    spec = ExecSpec(container, identity(), ("python", "-c", program), workdir="/tmp", **options)
    handle = runtime.create(spec)
    process = await OwnedDockerProcess.start(runtime, handle, spec)
    return process, spec


async def line(process):
    return await asyncio.wait_for(process.stdout.readline(), 10)


def test_completed_exec_retains_rootfs_and_daemon_for_grader(environment):
    async def scenario():
        runtime, owned, container, native = environment
        native.exec_run(
            [
                "python",
                "-c",
                'import os,time;open("/tmp/unrelated.pid","w").write(str(os.getpid()));time.sleep(600)',
            ],
            detach=True,
        )
        process, _ = await start(
            environment,
            """
import os,time
assert open(os.environ['CONFIG']).read()=='private-fixture'
try: open('/proc/%d/fd/0'%os.getppid()).read()
except PermissionError: pass
else: raise AssertionError('Control input was exposed to the child')
child=os.fork()
if child==0:
 os.setsid();open('/tmp/owned.pid','w').write(str(os.getpid()))
 while True: time.sleep(1)
while not os.path.exists('/tmp/owned.pid'): time.sleep(.01)
open('/tmp/result.txt','w').write('retained')
print('{"result":"ready"}',flush=True)
""",
            private_inputs={"CONFIG": b"private-fixture"},
        )
        try:
            assert json.loads(await line(process)) == {"result": "ready"}
            assert await asyncio.wait_for(process.wait(), 10) == 0
            assert await line(process) == b""
            status = owned.control(process.handle, "status")
            assert status["main_done"] and status["children"] == 1
            expired = owned.control(process.handle, "expire")
            assert expired["main_done"] and not expired["stopped"] and expired["children"] == 1
            assert (
                native.exec_run(
                    [
                        "python",
                        "-c",
                        'import os;assert open("/tmp/result.txt").read()=="retained";os.kill(int(open("/tmp/owned.pid").read()),0)',
                    ]
                ).exit_code
                == 0
            )
            with pytest.raises(ManagedRuntimeError):
                owned.control(replace(process.handle, control_key=os.urandom(32)), "stop")
            assert owned.control(process.handle, "stop")["children"] == 0
            assert (
                native.exec_run(
                    [
                        "python",
                        "-c",
                        'import os,glob;os.kill(int(open("/tmp/unrelated.pid").read()),0);assert not glob.glob("/tmp/evalhive-private-*")',
                    ]
                ).exit_code
                == 0
            )
            assert (await process.release())["stopped"]
            deadline = time.monotonic() + 5
            while owned.inspect(process.handle)["running"]:
                assert time.monotonic() < deadline
                await asyncio.sleep(0.05)
            assert runtime.inspect(container)["status"] == "running"
        finally:
            await process.aclose()

    asyncio.run(scenario())


def test_cancel_kills_detached_term_resistant_descendants(environment):
    async def scenario():
        runtime, owned, container, _ = environment
        process, _ = await start(
            environment,
            """
import os,signal,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
if os.fork()==0:
 os.setsid()
 while True: time.sleep(1)
print('{"running":true}',flush=True)
while True: time.sleep(1)
""",
        )
        try:
            assert json.loads(await line(process))["running"]
            process.terminate()
            await asyncio.wait_for(asyncio.gather(*process._stop_tasks), 8)
            assert await asyncio.wait_for(process.wait(), 8) == 137
            assert owned.control(process.handle, "status")["children"] == 0
            assert runtime.inspect(container)["status"] == "running"
            await process.release()
        finally:
            await process.aclose()

    asyncio.run(scenario())


def test_nonroot_private_inputs_and_native_workdir(environment):
    async def scenario():
        process, _ = await start(
            environment,
            """
import os,stat
path=os.environ['CONFIG'];assert stat.S_IMODE(os.stat(path).st_mode)==0o600
assert open(path).read()=='private-fixture';assert os.getuid()==65534
assert os.getcwd()=='/tmp'
print('{"nonroot":true}',flush=True)
""",
            user="65534:65534",
            private_inputs={"CONFIG": b"private-fixture"},
        )
        try:
            assert json.loads(await line(process))["nonroot"]
            assert await asyncio.wait_for(process.wait(), 10) == 0
            await process.release()
        finally:
            await process.aclose()

    asyncio.run(scenario())


def test_cancel_is_independent_of_backpressured_output(environment):
    async def scenario():
        process, _ = await start(
            environment,
            """
import os,time
print('{"started":true}',flush=True)
while True:
 os.write(1,b'x'*60000+b'\\n');os.write(2,b'e'*60000)
""",
        )
        try:
            assert json.loads(await line(process))["started"]
            await asyncio.sleep(0.5)  # Deliberately stop consuming the bounded stream.
            process.terminate()
            await asyncio.wait_for(asyncio.gather(*process._stop_tasks), 8)
            count = 0
            while value := await line(process):
                count += len(value)
            assert count > 0
            assert await asyncio.wait_for(process.wait(), 5) in (137, 143)
            assert len(process.stderr) <= 65536 and process.stderr_truncated
            await process.release()
        finally:
            await process.aclose()

    asyncio.run(scenario())


def test_supervisor_death_marks_environment_unsafe_and_stops_it(environment):
    async def scenario():
        runtime, _, container, native = environment
        process, _ = await start(
            environment,
            """
import os,time
print('{"supervisor":%d}'%os.getppid(),flush=True)
while True: time.sleep(1)
""",
        )
        try:
            parent = json.loads(await line(process))["supervisor"]
            assert (
                native.exec_run(
                    ["python", "-c", f"import os,signal;os.kill({parent},signal.SIGKILL)"]
                ).exit_code
                == 0
            )
            with pytest.raises(ManagedRuntimeError, match="unsafe to grade"):
                await asyncio.wait_for(process.wait(), 10)
            assert process.environment_broken
            assert runtime.inspect(container)["status"] == "exited"
        finally:
            await process.aclose()

    asyncio.run(scenario())
