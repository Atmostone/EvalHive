"""A network-isolated rootless Task reaches only its authorized model socket."""

import asyncio
import json
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import docker
import httpx
import pytest
from app.benchmarks.model_channel import AgentModelChannel, model_connection
from app.runtime.broker import ResourceLease
from app.runtime.managed_docker import RootlessDockerRuntime
from app.runtime.managed_spec import ContainerSpec, OwnerScope, PathMapping, Quota, RuntimePolicy
from app.runtime.registered_assets import CompiledEnvironment

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_MODEL_CHANNEL") != "1",
    reason="Requires explicitly scoped rootless model channel fixtures",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("user", ["0", "65532:65532"])
async def test_owned_unix_model_channel_crosses_network_namespace_without_provider_credentials(user):
    suffix = "model-channel-" + uuid.uuid4().hex
    local = Path(os.environ["EVALHIVE_NATIVE_FIXTURE_DIR"]) / suffix
    local.mkdir()
    host = os.environ["EVALHIVE_NATIVE_FIXTURE_HOST"] + "/" + suffix
    client = docker.DockerClient(base_url="unix:///run/user/11001/docker.sock", timeout=15)
    runtime = RootlessDockerRuntime(client, RuntimePolicy(PathMapping(host, str(local))))
    scope = OwnerScope(*(str(uuid.uuid4()) for _ in range(3)), 1)
    model = {"model": {"name": "fixture", "endpoint": "http://provider.invalid/v1", "credential_ref": "model"}}
    route, private = model_connection(model, {"model": "owner-credential-never-in-task"})
    program = """
import errno,http.client,json,os,socket
class UnixHTTP(http.client.HTTPConnection):
 def connect(self):
  self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
  self.sock.settimeout(10)
  self.sock.connect('/evalhive-control/model.sock')
connection=UnixHTTP('owned-model')
assert os.getuid()==int(os.environ['EXPECTED_UID'])
connection.request('POST','/agent/v1/chat/completions',json.dumps({'model':'fixture','stream':True}),
 headers={'Authorization':'Bearer '+os.environ['ROLE_TOKEN'],'Content-Type':'application/json'})
response=connection.getresponse()
assert response.status==200,response.status
assert response.read()==b'data: fixture\\n\\ndata: [DONE]\\n\\n'
try: open('/evalhive-control/foreign-file','w')
except OSError as error: assert error.errno in (errno.EROFS,errno.EACCES)
else: raise AssertionError('Control mount was writable')
print(json.dumps({'model':'fixture','uid':os.getuid(),'received':True}))
"""
    image = await asyncio.to_thread(runtime.resolve_image, "evalhive-environment-workspace:0.1.0")
    handle = str(uuid.uuid4())
    task = ResourceLease(handle, scope, "container", ContainerSpec(
        scope, handle, image["image_id"], command=("python", "-c", program), user=user,
        environment={"ROLE_TOKEN": private["model"], "EXPECTED_UID": "0" if user == "0" else "65532"}, quota=Quota(256, 0.25, 64),
    ))
    compiled = CompiledEnvironment(scope, {"task": ("rootless", task)}, (), {}, {}, "fixture", {}, {})
    channel = AgentModelChannel(object(), runtime, route)
    channel._guard = AsyncMock(return_value=30)
    compiled, _ = channel.bind(compiled, {"agent": {"mode": "in_environment", "environment_handle": "task"}}, "rootless")
    observed = []

    def provider(request):
        observed.append(request)
        return httpx.Response(200, content=b"data: fixture\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    native = None
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        original = channel.listener.app.client
        await original.aclose()
        channel.listener.app.client = upstream
        channel.listener.app.owns_client = False
        try:
            await channel.start()
            native = await asyncio.to_thread(runtime.create, compiled.leases["task"][1].intent)
            container = await asyncio.to_thread(runtime.container, native)
            state = await asyncio.wait_for(asyncio.to_thread(container.wait, timeout=15), 20)
            assert state["StatusCode"] == 0, state
            assert container.attrs["Config"]["NetworkDisabled"]
            assert len(observed) == 1
            assert observed[0].headers["authorization"] == "Bearer " + route.api_key
            assert route.api_key not in json.dumps(task.intent.environment)
            channel._guard.assert_awaited()
        finally:
            if native is not None:
                await asyncio.to_thread(runtime.stop, native, grace_seconds=0, remove=True)
            await channel.close()
            client.close()
