"""Registered bundles attach only to the selected native task, with host-only inputs."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.benchmarks.harness_transport import RootlessHarnessTransport
from app.runtime.broker import ResourceLease
from app.runtime.bundles import RuntimeBundle, validate_bundle
from app.runtime.managed_spec import ContainerSpec, ManagedRuntimeError
from app.runtime.spec import BindMount

from tests.runtime.test_registered_assets import IMAGE, plan, scope
from tests.unit.test_runtime_bundles import receipt


def setup(tmp_path):
    value = receipt(tmp_path)
    manifest = {"id": "example", "version": "1.0"}
    _, fingerprint = validate_bundle(tmp_path, manifest)
    bundle = RuntimeBundle(
        IMAGE,
        fingerprint,
        "/installed/owned-bundle",
        str(tmp_path),
        tuple(value["entrypoint"]),
        "example",
        "1.0",
        (),
    )
    origin = scope()
    handle, attached = str(uuid.uuid4()), str(uuid.uuid4())
    intent = ContainerSpec(
        origin, handle, IMAGE, mounts=(BindMount(bundle.host_path, "/evalhive-runtime"),)
    )
    lease = ResourceLease(handle, origin, "container", intent)
    native = plan()
    prepared = {
        "ready": True,
        "session_handle": handle,
        "native_state": {},
        "artifacts": [],
        "agent": {**native["agent"], "environment_handle": handle, "cwd": "/native-task"},
    }
    process = object()
    broker = SimpleNamespace(
        handle=AsyncMock(return_value={"handle": attached}),
        open_transport=AsyncMock(return_value=process),
    )

    def request(selected_scope, operation, *, handle, payload=None, request_id):
        return {
            "scope": selected_scope,
            "operation": operation,
            "handle": handle,
            "payload": payload or {},
            "id": request_id,
        }

    host = SimpleNamespace(
        purpose="execution",
        assets=SimpleNamespace(provider_id="fixture.rootless"),
        environment=SimpleNamespace(leases={"task": ("fixture.rootless", lease)}),
        host_private_inputs={"agent.credentials": b"private-fixture-value"},
        scope=origin,
        host_broker=broker,
        request=request,
    )
    return host, native, prepared, bundle, manifest, process


def test_registered_entrypoint_native_cwd_and_private_file_reference_reach_owned_broker(tmp_path):
    async def scenario():
        host, native, prepared, bundle, manifest, process = setup(tmp_path)
        transport = RootlessHarnessTransport(
            host, native, prepared, bundle, manifest, credential_ref="agent.credentials"
        )
        assert (await transport()).process is process
        call = host.host_broker.handle.await_args.args[0]
        assert call["handle"] == prepared["agent"]["environment_handle"]
        assert call["payload"] == {
            "argv": list(bundle.entrypoint),
            "cwd": "/native-task",
            "mode": "attach",
            "private_input_refs": {"EVALHIVE_CREDENTIALS_FILE": "agent.credentials"},
        }
        assert "private-fixture-value" not in str(call)
        assert host.host_broker.open_transport.await_args.args[0]["id"] == "harness.attach"
        with pytest.raises(ManagedRuntimeError, match="reattach"):
            await transport()
        host.host_broker.handle.assert_awaited_once()

    asyncio.run(scenario())


def test_main_exit_waits_for_confirmed_descendant_shutdown_even_after_caller_cancellation():
    from app.benchmarks.harness_transport import QuiescentHarnessProcess

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        process = SimpleNamespace(wait=AsyncMock(return_value=0))

        async def stop():
            entered.set()
            await release.wait()

        stopped = AsyncMock(side_effect=stop)
        wrapped = QuiescentHarnessProcess(process, stopped)
        pending = asyncio.create_task(wrapped.wait())
        await entered.wait()
        assert not pending.done()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        release.set()
        assert await wrapped.wait() == 0
        process.wait.assert_awaited_once()
        stopped.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    ["foreign", "provider", "service", "grader", "writable", "bundle", "credentials", "evaluation"],
)
def test_invalid_target_or_private_grant_is_rejected_before_any_native_effect(tmp_path, change):
    host, native, prepared, bundle, manifest, _ = setup(tmp_path)
    credential_ref = "agent.credentials"
    provider, lease = host.environment.leases["task"]
    if change == "foreign":
        prepared["agent"]["environment_handle"] = str(uuid.uuid4())
    elif change == "provider":
        provider = "foreign.provider"
    elif change in {"service", "grader"}:
        lease = replace(lease, intent=replace(lease.intent, role=change))
    elif change == "writable":
        lease = replace(
            lease,
            intent=replace(
                lease.intent, mounts=(BindMount(bundle.host_path, "/evalhive-runtime", False),)
            ),
        )
    elif change == "bundle":
        bundle = replace(bundle, host_path="/installed/another-bundle")
    elif change == "credentials":
        credential_ref = "worker.credentials"
    else:
        host.purpose = "evaluation"
    host.environment.leases["task"] = (provider, lease)
    with pytest.raises(ManagedRuntimeError):
        RootlessHarnessTransport(
            host, native, prepared, bundle, manifest, credential_ref=credential_ref
        )
    host.host_broker.handle.assert_not_awaited()


def test_changed_runtime_files_are_rejected_before_starting_the_harness(tmp_path):
    async def scenario():
        host, native, prepared, bundle, manifest, _ = setup(tmp_path)
        transport = RootlessHarnessTransport(host, native, prepared, bundle, manifest)
        (tmp_path / "node").chmod(0o755)
        (tmp_path / "node").write_bytes(b"changed")
        with pytest.raises(ManagedRuntimeError):
            await transport()
        host.host_broker.handle.assert_not_awaited()

    asyncio.run(scenario())


def test_prepared_task_is_copied_without_rewriting_the_native_environment_paths(tmp_path):
    host, native, prepared, bundle, manifest, _ = setup(tmp_path)
    original = deepcopy(prepared)
    RootlessHarnessTransport(host, native, prepared, bundle, manifest)
    assert prepared == original


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["success", "cancelled", "failed", "expired", "cleaning"])
async def test_registered_service_retention_requires_success_current_ownership_and_live_trial(tmp_path, condition):
    host, native, prepared, bundle, manifest, _ = setup(tmp_path)
    native["agent"]["process_policy"] = prepared["agent"]["process_policy"] = "retain_until_cleanup"
    native["requirements"]["capabilities"] = ["host.retained_agent_processes_v1"]
    resource = next(item for item in native["resources"] if item["id"] == native["agent"]["environment_handle"])
    resource["config"]["asset_id"] = "retained.task"
    host.assets.document = {"assets": {"retained.task": {"agent_process_policy": "retain_until_cleanup"}}}
    now = datetime.now(UTC)

    @asynccontextmanager
    async def transaction():
        yield None, SimpleNamespace(
            phase="cleaning" if condition == "cleaning" else "driving",
            cancel_requested_at=now if condition == "cancelled" else None,
            deadline_at=now + timedelta(seconds=-1 if condition == "expired" else 60),
        ), SimpleNamespace(status="failed" if condition == "failed" else "success"), now

    host.agent_gateway = SimpleNamespace(calls=SimpleNamespace(runner_transaction=transaction))
    process = SimpleNamespace(wait=AsyncMock(return_value=0))
    host.host_broker.open_transport.return_value = process
    attached = str(uuid.uuid4())
    host.host_broker.handle.side_effect = [
        {"handle": attached},
        {"handle": attached, "native": {"main_done": True}} if condition == "success" else {"handle": attached, "stopped": True},
    ]
    wrapped = await RootlessHarnessTransport(host, native, prepared, bundle, manifest)()
    assert await wrapped.wait() == 0
    last = host.host_broker.handle.await_args.args[0]
    assert last["id"] == ("harness.completed" if condition == "success" else "harness.quiesce")
    assert await wrapped.wait() == 0
    assert host.host_broker.handle.await_count == 2


def test_prepared_access_cannot_change_the_registered_process_policy(tmp_path):
    host, native, prepared, bundle, manifest, _ = setup(tmp_path)
    prepared["agent"]["process_policy"] = "retain_until_cleanup"
    with pytest.raises(ManagedRuntimeError, match="process policy"):
        RootlessHarnessTransport(host, native, prepared, bundle, manifest)
    host.host_broker.handle.assert_not_awaited()
