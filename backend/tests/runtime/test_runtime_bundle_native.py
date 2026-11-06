"""Pinned bundles run unchanged in two distinct task root filesystems."""

import asyncio
import io
import json
import os
import tarfile
import uuid
from contextlib import suppress
from pathlib import Path

import docker
import pytest
from app.runtime.bundle_probe import check_bundle
from app.runtime.bundles import check_mount_target, install_bundle
from app.runtime.managed_docker import RootlessDockerRuntime
from app.runtime.managed_exec import OwnedDockerProcess, OwnedExecRuntime
from app.runtime.managed_spec import (
    ContainerSpec,
    ExecSpec,
    OwnerScope,
    PathMapping,
    Quota,
    RuntimePolicy,
)
from app.runtime.spec import BindMount
from evalhive_harness.client import ConnectorClient

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_BUNDLES") != "1",
    reason="Requires explicit native bundle engineering checks",
)
ROOT = Path(__file__).resolve().parents[3]


def identity():
    return str(uuid.uuid4())


@pytest.fixture(scope="module", params=["pi", "opencode"])
def bundle(request, tmp_path_factory):
    connector = request.param
    declaration = json.loads((ROOT / "harnesses" / connector / "manifest.json").read_text())
    declaration["requirements"]["runtime_bundle"]["image"] = (
        f"evalhive-{connector}-bundle:development"
    )
    client = docker.DockerClient(base_url="unix:///run/user/11001/docker.sock")
    base = tmp_path_factory.mktemp("bundle-" + connector)
    data = base / "data"
    installed = Path(os.getenv("EVALHIVE_BUNDLE_TEST_CACHE", str(base / "installed")))
    data.mkdir()
    mapping = PathMapping(str(installed), str(installed))
    runtime = RootlessDockerRuntime(client, RuntimePolicy(PathMapping(str(data), str(data))))
    result = install_bundle(runtime, declaration, mapping)
    assert install_bundle(runtime, declaration, mapping) == result
    runtime = RootlessDockerRuntime(
        client, RuntimePolicy(PathMapping(str(data), str(data)), (mapping,))
    )
    try:
        yield connector, runtime, result
    finally:
        client.close()


@pytest.fixture(params=["workspace", "postgres"])
def task(bundle, request):
    connector, runtime, installed = bundle
    image_name = (
        "evalhive-environment-workspace:0.1.0" if request.param == "workspace" else "postgres:16"
    )
    image = runtime.resolve_image(image_name)["image_id"]
    check_mount_target(runtime, image)
    container = runtime.create(
        ContainerSpec(
            OwnerScope(identity(), identity(), identity(), 1),
            identity(),
            image,
            entrypoint=(),
            command=("/bin/sleep", "600"),
            workdir="/tmp",
            user="0:0",
            mounts=(BindMount(installed.host_path, "/evalhive-runtime"),),
            quota=Quota(2048, 1, 256),
        )
    )
    native = runtime.client.containers.get(container.container_id)
    assert (
        native.exec_run(
            ["/bin/sh", "-c", "mkdir /tmp/task && printf initial > /tmp/task/seed"]
        ).exit_code
        == 0
    )
    try:
        yield connector, runtime, installed, container, native, request.param
    finally:
        runtime.stop(container, grace_seconds=0, remove=True)


FAKE_SERVER = r"""
import {createServer} from 'node:http';
import {readFileSync} from 'node:fs';
let count=0;
const command=readFileSync('/tmp/task/command','utf8');
const server=createServer(async(req,res)=>{
 let raw='';for await(const chunk of req) raw+=chunk;
 const body=JSON.parse(raw);const first=++count===1;
 if(first && !body.tools.some(tool=>tool.function.name==='bash')){
  res.writeHead(400);res.end('Expected native bash tool');return;
 }
 res.writeHead(200,{'Content-Type':'text/event-stream'});
 const send=value=>res.write('data: '+JSON.stringify({id:'fixture-'+count,
  object:'chat.completion.chunk',created:1,model:'fake-model',...value})+String.fromCharCode(10,10));
 send({choices:[{index:0,delta:{role:'assistant',...(first?{tool_calls:[{index:0,id:'native-shell',type:'function',
  function:{name:'bash',arguments:JSON.stringify({command,description:'Verify native task dependencies'})}}]}:{content:'Completed'})},finish_reason:null}]});
 send({choices:[{index:0,delta:{},finish_reason:first?'tool_calls':'stop'}]});
 send({choices:[],usage:{prompt_tokens:10,completion_tokens:2,total_tokens:12}});
 res.end('data: [DONE]'+String.fromCharCode(10,10));
});
server.listen(0,'127.0.0.1',()=>console.log(JSON.stringify({url:'http://127.0.0.1:'+server.address().port+'/v1'})));
"""


def put_files(native, files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content in files.items():
            value = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(value)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(value))
    assert native.put_archive("/tmp/task", stream.getvalue())


def test_native_harness_shell_uses_task_dependencies_and_retains_files(task, record_property):
    async def scenario():
        connector, runtime, installed, container, native, variant = task
        record_property("bundle_image_id", installed.image_id)
        record_property("bundle_fingerprint", installed.fingerprint)
        record_property("task_image_id", container.image_id)
        compatibility = await check_bundle(
            runtime, container, installed, [identity() for _ in range(2 + len(installed.probes))]
        )
        assert compatibility["compatible"]
        assert compatibility["runtime_admission"] == "pending"
        command = (
            'test -f seed && python -c \'import sys; print("task-python:"+str(sys.version_info.major)+"."+str(sys.version_info.minor))\' > result.txt'
            if variant == "workspace"
            else "test -f seed && psql --version > result.txt"
        )
        put_files(native, {"fake.mjs": FAKE_SERVER, "command": command})
        owned = OwnedExecRuntime(runtime)

        async def launch(argv, **options):
            spec = ExecSpec(container, identity(), tuple(argv), workdir="/tmp/task", **options)
            handle = owned.create(spec)
            return await OwnedDockerProcess.start(owned, handle, spec)

        endpoint = await launch(["/evalhive-runtime/node", "/tmp/task/fake.mjs"])
        process, client = None, None
        try:
            address = json.loads(await asyncio.wait_for(endpoint.stdout.readline(), 10))["url"]
            process = await launch(
                installed.entrypoint,
                private_inputs={"EVALHIVE_CREDENTIALS_FILE": b'{"model_api_key":"local-fixture"}'},
                environment={"HOME": "/tmp", "TMPDIR": "/tmp"},
            )
            client = ConnectorClient(process)
            declaration = await client.request("describe")
            assert declaration["id"] == connector and declaration["version"] == "0.2.0"
            run = {
                "protocol_version": "1.0",
                "workspace_id": container.scope.workspace_id,
                "task_id": container.scope.trial_id,
                "attempt_id": identity(),
                "task": {"prompt": "Run the requested native shell check and finish."},
                "cwd": "/tmp/task",
                "config": {},
                "limits": {"max_turns": 4},
                "environment": {
                    "id": "fixture",
                    "resources": [{"id": "tools", "kind": "tools", "config": {"names": ["bash"]}}],
                },
                "model": {
                    "name": "fake-model",
                    "endpoint": address,
                    "credential_ref": "model_api_key",
                },
            }
            assert (await client.request("validate", {"run": run}))["accepted"]

            async def collect():
                events = []
                async for event in client.events():
                    events.append(event)
                    if event["event"] == "run.finished":
                        return events
                raise AssertionError("Native harness exited without completion")

            collecting = asyncio.create_task(collect())
            try:
                await client.request("start", {"run": run})
                events = await asyncio.wait_for(collecting, 90)
                assert events[-1]["data"]["status"] == "success"
                assert any(event["event"] == "tool.end" for event in events)
            finally:
                collecting.cancel()
                await asyncio.gather(collecting, return_exceptions=True)
            output = native.exec_run(["/bin/cat", "/tmp/task/result.txt"])
            assert output.exit_code == 0
            assert (
                output.output.startswith(b"task-python:3.12")
                if variant == "workspace"
                else b"PostgreSQL) 16." in output.output
            )
            await client.close()
            assert await process.wait() == 0
            assert runtime.inspect(container)["status"] == "running"
            assert owned.control(process.handle, "status")["main_done"]
        finally:
            if client is not None:
                with suppress(Exception):
                    await client.close()
            if process is not None:
                with suppress(Exception):
                    await process.release()
                await process.aclose()
            endpoint.terminate()
            with suppress(Exception):
                await asyncio.wait_for(asyncio.gather(*endpoint._stop_tasks), 8)
                await endpoint.release()
            await endpoint.aclose()

    asyncio.run(scenario())


def test_musl_task_fails_compatibility_before_a_model_request(bundle, record_property):
    async def scenario():
        _, runtime, installed = bundle
        image = runtime.resolve_image("alpine:latest")["image_id"]
        record_property("bundle_image_id", installed.image_id)
        record_property("task_image_id", image)
        check_mount_target(runtime, image)
        container = runtime.create(
            ContainerSpec(
                OwnerScope(identity(), identity(), identity(), 1),
                identity(),
                image,
                entrypoint=(),
                command=("/bin/sleep", "600"),
                mounts=(BindMount(installed.host_path, "/evalhive-runtime"),),
                quota=Quota(256, 1, 128),
            )
        )
        try:
            result = await check_bundle(
                runtime,
                container,
                installed,
                [identity() for _ in range(2 + len(installed.probes))],
            )
            assert result == {"compatible": False, "reason": "task_libc_unavailable"}
            assert runtime.inspect(container)["status"] == "running"
        finally:
            runtime.stop(container, grace_seconds=0, remove=True)

    asyncio.run(scenario())
