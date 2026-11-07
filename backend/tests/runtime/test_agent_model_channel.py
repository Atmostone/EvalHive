"""An owned model route is private, admitted, and bound only to the agent Task."""

import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.benchmarks.model_channel import AgentModelChannel, model_connection
from app.runtime.broker import ResourceLease
from app.runtime.managed_spec import (
    ContainerSpec,
    ManagedRuntimeError,
    OwnerScope,
    PathMapping,
    RuntimePolicy,
)
from app.runtime.model_gateway import GatewayRejected
from app.runtime.registered_assets import CompiledEnvironment
from app.runtime.spec import BindMount

from tests.runtime.test_benchmark_composition import MANIFEST, setup
from tests.runtime.test_registered_assets import IMAGE
from tests.runtime.test_worker_host_purpose import invocation


def inputs():
    return ({"model": {"name": "fixture", "endpoint": "http://provider.invalid/v1", "credential_ref": "model"}},
            {"model": "private-owner-key", "mcp": "other-resource-key"})


def test_private_model_token_replaces_only_the_selected_task_credential():
    spec, credentials = inputs()
    route, private = model_connection(spec, credentials)
    assert route.api_key == credentials["model"]
    assert private["model"] == route.token != route.api_key
    assert private["mcp"] == credentials["mcp"]
    assert credentials["model"] == "private-owner-key"
    assert route.api_key not in repr(route) and route.token not in repr(route)


def environment():
    scope = OwnerScope(*(str(uuid.uuid4()) for _ in range(3)), 1)
    task = ResourceLease(str(uuid.uuid4()), scope, "container", ContainerSpec(
        scope, "task", IMAGE, role="environment"
    ))
    world = replace(task, handle=str(uuid.uuid4()), intent=replace(task.intent, role="service"))
    compiled = CompiledEnvironment(scope, {"task": ("rootless", task), "world": ("rootless", world)},
                                   (), {}, {}, "source-fingerprint", {}, {})
    return compiled, {"agent": {"mode": "in_environment", "environment_handle": "task"}}


@pytest.mark.asyncio
async def test_model_listener_binds_only_the_admitted_task_and_is_lazy(tmp_path):
    spec, credentials = inputs()
    route, _ = model_connection(spec, credentials)
    runtime = SimpleNamespace(policy=RuntimePolicy(PathMapping(str(tmp_path), str(tmp_path))))
    channel = AgentModelChannel(object(), runtime, route)
    compiled, plan = environment()
    bound, descriptor = channel.bind(compiled, plan, "rootless")
    assert not channel.listener.directory.exists()
    assert bound.leases["world"] == compiled.leases["world"]
    assert compiled.leases["task"][1].intent.mounts == ()
    mount, = bound.leases["task"][1].intent.mounts
    assert mount.read_only and mount.target == "/evalhive-control"
    assert compiled.scope.session_id in mount.source
    assert descriptor == {"transport": "unix_http_v1", "socket_path": "/evalhive-control/model.sock", "role": "agent"}
    assert bound.fingerprint != compiled.fingerprint and bound.files == compiled.files
    with pytest.raises(ManagedRuntimeError):
        channel.bind(compiled, plan, "rootless")
    await channel.start()
    assert channel.listener.socket_path.is_socket()
    await channel.close()
    assert not channel.listener.directory.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mount", ["/", "/evalhive-control", "/evalhive-control/nested"])
async def test_registered_mounts_cannot_shadow_the_control_channel(tmp_path, mount):
    route, _ = model_connection(*inputs())
    channel = AgentModelChannel(object(), SimpleNamespace(policy=None), route)
    compiled, plan = environment()
    provider, task = compiled.leases["task"]
    compiled.leases["task"] = provider, replace(task, intent=replace(
        task.intent, mounts=(BindMount(str(tmp_path), mount, True),)
    ))
    with pytest.raises(ManagedRuntimeError, match="overlaps"):
        channel.bind(compiled, plan, "rootless")
    assert channel.listener is None


@pytest.mark.asyncio
async def test_model_guard_checks_current_attempt_and_its_earlier_deadline():
    route, _ = model_connection(*inputs())
    now = datetime.now(UTC)
    trial = SimpleNamespace(phase="driving", cancel_requested_at=None, deadline_at=now + timedelta(seconds=60))
    attempt = SimpleNamespace(status="running", started_at=now - timedelta(seconds=5), run_spec={"limits": {"wall_time_seconds": 20}})

    @asynccontextmanager
    async def transaction():
        yield None, trial, attempt, now

    channel = AgentModelChannel(SimpleNamespace(runner_transaction=transaction), None, route)
    assert await channel._guard("agent") == 15
    for name, value in [("phase", "capturing"), ("cancel_requested_at", now)]:
        original = getattr(trial, name)
        setattr(trial, name, value)
        with pytest.raises(GatewayRejected):
            await channel._guard("agent")
        setattr(trial, name, original)
    attempt.status = "success"
    with pytest.raises(GatewayRejected):
        await channel._guard("agent")
    with pytest.raises(GatewayRejected):
        await channel._guard("another-role")


def test_coordinator_replaces_model_key_before_admission_and_accounts_for_control_storage(tmp_path):
    coordinator, store, owner, assets, _, options = setup(tmp_path)
    spec, credentials = inputs()
    options["harness_spec"].update(spec)
    options["credentials"] = credentials
    driver = coordinator.execution(store, owner, MANIFEST, assets, model_gateway=True, **options)
    executor = driver.agent_control.executor
    private = json.loads(driver.host.host_private_inputs["agent.credentials"])
    assert private["model"] != credentials["model"]
    assert private["mcp"] == credentials["mcp"]
    assert executor.model_channel.listener is None
    assert credentials["model"] not in driver.host.host_private_inputs["agent.credentials"].decode()
    assert driver.host.host_auxiliary_disk_mb == executor.spool_disk_mb + 1
    store.reserve.assert_not_awaited()
    assets.compile.assert_not_awaited()


@pytest.mark.asyncio
async def test_detach_closes_the_model_service_even_when_worker_detach_fails(tmp_path):
    coordinator, store, owner, assets, _, options = setup(tmp_path)
    host = coordinator.execution(store, owner, MANIFEST, assets, **options).host
    host.channel = SimpleNamespace(detach=AsyncMock(side_effect=RuntimeError("Fixture failure")))
    close = host.agent_gateway.executor.detach_environment = AsyncMock()
    with pytest.raises(RuntimeError):
        await host.detach()
    close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted", [True, False])
async def test_channel_binding_and_start_follow_admission_and_durable_installation(monkeypatch, admitted):
    host, spec, plan = invocation(False)
    order = []

    async def reserve(*args, **kwargs):
        order.append("reserve")
        if not admitted:
            raise ManagedRuntimeError("Fixture admission rejected")

    def bind(compiled, selected_plan):
        order.append("bind")
        return compiled

    async def install(*args, **kwargs):
        order.append("install")

    async def start():
        order.append("start")

    host.store.reserve.side_effect = reserve
    host.agent_gateway = SimpleNamespace(executor=SimpleNamespace(
        bind_environment=bind, start_environment=start,
    ))
    monkeypatch.setattr("app.benchmarks.worker_host.install_session", install)
    if admitted:
        await host.install_plan(spec, plan)
        assert order == ["reserve", "bind", "install", "start"]
    else:
        with pytest.raises(ManagedRuntimeError):
            await host.install_plan(spec, plan)
        assert order == ["reserve"]
