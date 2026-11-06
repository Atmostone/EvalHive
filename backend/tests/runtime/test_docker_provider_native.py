"""Real SDK worker has no backend/Docker access and uses only owned resource RPC."""

import asyncio
import io
import json
import os
import tarfile
import uuid
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

import pytest
from app.runtime.admission import GroupPolicy, ResourceDemand, check_group, host_capacity
from app.runtime.broker import ExportGrant, ResourceLease
from app.runtime.bundles import install_bundle
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.managed_docker import RootlessDockerRuntime
from app.runtime.managed_spec import ContainerSpec, PathMapping, Quota
from app.runtime.spec import BindMount
from docker.errors import NotFound
from evalhive_benchmark import ConnectorClient, ProtocolError
from evalhive_harness.client import ConnectorClient as HarnessClient

from tests.runtime.authority_fixture import FixtureArtifacts, FixtureAuthority, manifest
from tests.runtime.test_owned_exec_native import environment  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_PROVIDER") != "1",
    reason="Requires explicit native rootless provider checks",
)


def identity():
    return str(uuid.uuid4())


def request(scope, operation, payload=None, *, handle=None):
    result = {
        "protocol_version": "1.0",
        "type": "broker_request",
        "id": identity(),
        "scope": {
            "workspace_id": scope.workspace_id,
            "trial_id": scope.trial_id,
            "generation": scope.generation,
        },
        "provider": "fixture.rootless",
        "operation": operation,
        "payload": payload or {},
    }
    if handle is not None:
        result["handle"] = handle
    return result


def test_isolated_benchmark_worker_prepares_captures_and_releases_through_sdk(environment):  # noqa: F811
    async def scenario():
        runtime, _, original, _ = environment
        # Reuse the scoped helper installation; the SDK itself must allocate the task.
        runtime.stop(original, grace_seconds=0, remove=True)
        scope = original.scope
        helper = runtime.policy.readonly[0].host
        task_spec = ContainerSpec(
            scope,
            identity(),
            original.image_id,
            command=("python", "-c", "import time;time.sleep(300)"),
            mounts=(BindMount(helper, "/evalhive-runtime"),),
            quota=Quota(256, 1, 128),
        )
        worker_image = runtime.resolve_image("evalhive-broker-worker-fixture:development")[
            "image_id"
        ]
        worker_spec = ContainerSpec(
            scope, identity(), worker_image, role="worker", quota=Quota(512, 0.25, 128)
        )
        task = ResourceLease(identity(), scope, "container", task_spec)
        worker = ResourceLease(identity(), scope, "container", worker_spec)
        measured = host_capacity(runtime, Path(runtime.client.info()["DockerRootDir"]))
        eligibility = check_group(
            (
                ResourceDemand("task", task_spec.quota, 256, 1, 16),
                ResourceDemand("worker", worker_spec.quota, 512, 0.25, 16),
            ),
            {"memory_mb": 256, "cpu_cores": 1, "disk_mb": 16},
            measured,
            runtime.policy,
            GroupPolicy(),
            active_trials=0,
        )
        assert eligibility["eligible"] and not eligibility["can_run"]
        # This explicit fixture authority tests RPC, not PostgreSQL durability.
        authority = FixtureAuthority(
            scope,
            {"task": task, "worker": worker},
            private_inputs={
                "fixture.alpha": b"alpha-private-fixture",
                "fixture.zeta": b"zeta-private-fixture",
            },
            outputs={
                (task.handle, "result"): ExportGrant(
                    "result", "/workspace/result.txt", "user", 2 * 1024**2
                )
            },
        )
        artifacts = FixtureArtifacts()
        provider = RootlessDockerProvider(runtime, authority, manifest(), artifacts=artifacts)
        client, process = None, None
        try:
            allocated = await provider.handle(request(scope, "allocate", {"resource_id": "worker"}))
            process = await provider.open_transport(
                request(scope, "inspect", handle=allocated["handle"])
            )
            client = ConnectorClient(process, broker=provider.handle)
            assert (await client.request("describe"))["id"] == "fixture.benchmark"
            case = await client.request(
                "resolve_case",
                {"case": {"dataset_id": "provider-fixture", "revision": "1", "case_id": "write"}},
            )
            spec = {
                "protocol_version": "1.0",
                "scope": {
                    "workspace_id": scope.workspace_id,
                    "trial_id": scope.trial_id,
                    "generation": scope.generation,
                },
                "case": case,
                "config": {},
                "credential_refs": {},
                "models": {},
                "limits": {
                    "wall_time_seconds": 30,
                    "memory_mb": 768,
                    "cpu_cores": 1.25,
                    "disk_mb": 32,
                },
            }
            plan = await client.request("plan", {"spec": spec})
            prepared = await client.request("prepare", {"spec": spec, "plan": plan})
            assert prepared["ready"] and prepared["native_state"]["isolated"]
            assert prepared["session_handle"] == task.handle
            exec_handle = prepared["native_state"]["exec_handle"]
            assert authority.resources[exec_handle].state == "retained"
            native_task = authority.resources[task.handle].native
            assert runtime.inspect(native_task)["status"] == "running"
            status = await provider.handle(request(scope, "inspect", handle=exec_handle))
            assert status["native"]["main_done"] and status["native"]["children"] == 1
            captured = await client.request(
                "capture", {"scope": spec["scope"], "payload": {"session_handle": task.handle}}
            )
            assert captured["artifacts"][0]["visibility"] == "user"
            with tarfile.open(fileobj=io.BytesIO(artifacts.values["result"])) as archive:
                assert (
                    archive.extractfile("result.txt").read()
                    == b"Prepared through the Benchmark SDK\n"
                )
            await client.close()
            assert process.returncode == 0
            assert runtime.inspect(native_task)["status"] == "running"
            assert (await provider.handle(request(scope, "release", handle=exec_handle)))[
                "released"
            ]
            assert (await provider.handle(request(scope, "release", handle=task.handle)))[
                "released"
            ]
            assert (await provider.handle(request(scope, "release", handle=worker.handle)))[
                "released"
            ]
        finally:
            if client is not None:
                with suppress(Exception):
                    await client.close()
            await provider.close()
            # A failed assertion must still remove every fixture-owned native resource.
            for lease in reversed(list(authority.resources.values())):
                if lease.native is None:
                    continue
                with suppress(Exception):
                    await provider.handle(request(scope, "release", handle=lease.handle))
                if lease.kind == "container":
                    with suppress(Exception):
                        runtime.stop(lease.native, grace_seconds=0, remove=True)
            if process is not None:
                await process.aclose()

    asyncio.run(scenario())


def test_native_supervisor_loss_is_fenced_then_quiesces_unsafe_environment(environment):  # noqa: F811
    async def scenario():
        runtime, _, original, _ = environment
        spec = ContainerSpec(original.scope, original.resource_id, original.image_id)
        lease = ResourceLease(identity(), original.scope, "container", spec, original, "allocated")
        authority = FixtureAuthority(original.scope, {"task": lease})
        provider = RootlessDockerProvider(runtime, authority, manifest())
        try:
            with pytest.raises(ProtocolError) as caught:
                await asyncio.wait_for(
                    provider.handle(
                        request(
                            original.scope,
                            "exec",
                            {
                                "argv": [
                                    "python",
                                    "-c",
                                    "import os,signal,time;os.kill(os.getppid(),signal.SIGKILL);time.sleep(30)",
                                ],
                                "timeout_seconds": 10,
                            },
                            handle=lease.handle,
                        )
                    ),
                    15,
                )
            assert caught.value.code == "ENVIRONMENT_UNSAFE"
            await asyncio.wait_for(
                asyncio.gather(*provider._safety_tasks, return_exceptions=False), 15
            )
            assert runtime.inspect(original)["status"] == "exited"
            assert ("save", lease.handle, "unsafe") in authority.journal
            assert authority.resources[lease.handle].state == "stopped"
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_private_service_dns_aliases_resolve_without_host_networking(environment):  # noqa: F811
    runtime, _, original, _ = environment
    runtime.stop(original, grace_seconds=0, remove=True)
    network = runtime.create_network(original.scope, identity())
    handles = []
    try:
        for aliases in [("database",), ()]:
            spec = ContainerSpec(
                original.scope,
                identity(),
                original.image_id,
                role="service",
                command=("python", "-c", "import time;time.sleep(300)"),
                network=network,
                network_aliases=aliases,
                quota=Quota(128, 0.5, 64),
            )
            handles.append(runtime.create(spec))
        native = runtime.client.containers.get(handles[-1].container_id)
        result = native.exec_run(
            ["python", "-c", "import socket;assert socket.gethostbyname('database')"]
        )
        assert result.exit_code == 0
        assert runtime.network(network).attrs["Internal"] is True
    finally:
        for handle in reversed(handles):
            runtime.stop(handle, grace_seconds=0, remove=True)
        runtime.remove_network(network)


@pytest.mark.parametrize("connector", ["pi", "opencode"])
def test_connector_container_uses_mcp_world_in_another_rootfs(environment, tmp_path, connector):  # noqa: F811
    async def scenario():
        runtime, _, world, _ = environment
        scope = world.scope
        repository = Path(__file__).resolve().parents[3]
        declaration = json.loads(
            (repository / "harnesses" / connector / "manifest.json").read_text()
        )
        declaration["requirements"]["runtime_bundle"]["image"] = (
            f"evalhive-{connector}-bundle:development"
        )
        root = Path(os.getenv("EVALHIVE_BUNDLE_TEST_CACHE", str(tmp_path / "bundles")))
        mapping = PathMapping(str(root), str(root))
        installed = install_bundle(runtime, declaration, mapping)
        runtime = RootlessDockerRuntime(
            runtime.client, replace(runtime.policy, readonly=(*runtime.policy.readonly, mapping))
        )
        credentials = (
            Path(runtime.policy.data.local)
            / "benchmark-sessions"
            / scope.session_id
            / "credentials.json"
        )
        credentials.parent.mkdir(parents=True)
        credentials.write_text('{"model_api_key":"local-fixture"}')
        credentials.chmod(0o600)
        host_credentials = str(
            Path(runtime.policy.data.host) / credentials.relative_to(runtime.policy.data.local)
        )
        spec = ContainerSpec(
            scope,
            identity(),
            world.image_id,
            role="worker",
            entrypoint=installed.entrypoint,
            command=(),
            workdir="/tmp",
            environment={
                "HOME": "/tmp",
                "TMPDIR": "/tmp",
                "EVALHIVE_CREDENTIALS_FILE": "/tmp/credentials.json",
            },
            namespace=world,
            mounts=(
                BindMount(installed.host_path, "/evalhive-runtime"),
                BindMount(host_credentials, "/tmp/credentials.json"),
            ),
            quota=Quota(2048, 1, 256),
        )
        worker = ResourceLease(identity(), scope, "container", spec)
        world_lease = ResourceLease(
            identity(),
            scope,
            "container",
            ContainerSpec(scope, world.resource_id, world.image_id),
            world,
            "allocated",
        )
        source = Path(__file__).with_name("mcp_environment_fixture.py").read_bytes()
        authority = FixtureAuthority(
            scope,
            {"world": world_lease, "worker": worker},
            private_inputs={"service.source": source},
        )
        authority.max_timeout = 120
        provider = RootlessDockerProvider(runtime, authority, manifest())
        client, service = None, None
        try:
            running = await provider.handle(
                request(
                    scope,
                    "exec",
                    {
                        "argv": [
                            "python",
                            "-c",
                            "import os;exec(compile(open(os.environ['SERVICE_SOURCE']).read(),'__main__','exec'),{'__name__':'__main__'})",
                        ],
                        "mode": "attach",
                        "timeout_seconds": 90,
                        "private_input_refs": {"SERVICE_SOURCE": "service.source"},
                    },
                    handle=world_lease.handle,
                )
            )
            service = await provider.open_transport(
                request(scope, "inspect", handle=running["handle"])
            )
            endpoint = json.loads(await asyncio.wait_for(service.stdout.readline(), 10))
            await provider.handle(request(scope, "allocate", {"resource_id": "worker"}))
            process = await provider.open_transport(request(scope, "inspect", handle=worker.handle))
            client = HarnessClient(process)
            assert (await client.request("describe"))["id"] == connector
            run = {
                "protocol_version": "1.0",
                "workspace_id": scope.workspace_id,
                "task_id": scope.trial_id,
                "attempt_id": identity(),
                "task": {"prompt": "Write the result using the MCP tool and finish."},
                "cwd": "/tmp",
                "config": {},
                "limits": {"max_turns": 4},
                "model": {
                    "name": "fake-model",
                    "endpoint": endpoint["model"],
                    "credential_ref": "model_api_key",
                },
                "environment": {
                    "id": "fixture",
                    "resources": [
                        {"id": "tools", "kind": "tools", "config": {"names": ["write_result"]}},
                        {
                            "id": "fixture",
                            "kind": "mcp",
                            "config": {
                                "transport": "http",
                                "url": endpoint["mcp"],
                                "aliases": {"write_result": "write_result"},
                            },
                        },
                    ],
                },
            }
            assert (await client.request("validate", {"run": run}))["accepted"]

            async def collect():
                async for event in client.events():
                    if event["event"] == "run.finished":
                        return event["data"]
                raise AssertionError("Harness ended without a terminal event")

            collecting = asyncio.create_task(collect())
            try:
                await client.request("start", {"run": run})
                result = await asyncio.wait_for(collecting, 60)
                assert result["status"] == "success", result.get("error", {}).get("code")
            finally:
                collecting.cancel()
                await asyncio.gather(collecting, return_exceptions=True)
            await client.close()
            native = runtime.client.containers.get(world.container_id)
            result = native.exec_run(
                [
                    "python",
                    "-c",
                    "from pathlib import Path;assert Path('/workspace/result.txt').read_text()=='Written inside the MCP environment\\n'",
                ]
            )
            assert result.exit_code == 0
            assert runtime.inspect(world)["status"] == "running"
            # A file created by the MCP world must not exist in the harness rootfs.
            worker_native = authority.resources[worker.handle].native
            check = runtime.client.containers.get(worker_native.container_id).get_archive
            with pytest.raises(NotFound):
                check("/workspace/result.txt")
        finally:
            if client is not None:
                with suppress(Exception):
                    await client.close()
            await provider.close()
            for lease in reversed(list(authority.resources.values())):
                if lease.native is not None:
                    with suppress(Exception):
                        await provider.handle(request(scope, "release", handle=lease.handle))
                    if lease.kind == "container":
                        with suppress(Exception):
                            runtime.stop(lease.native, grace_seconds=0, remove=True)
            if service is not None:
                await service.aclose()

    asyncio.run(scenario())
