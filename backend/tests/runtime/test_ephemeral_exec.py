"""Short commands release owned descendants only after confirmed completion."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from app.runtime.broker import ResourceLease
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.managed_exec import OwnedDockerProcess, OwnedExecRuntime
from app.runtime.managed_spec import (
    ContainerSpec,
    ManagedContainer,
    ManagedExec,
    OwnerScope,
    UncertainRuntimeError,
)
from evalhive_benchmark import ProtocolError

from tests.runtime.authority_fixture import FixtureAuthority, manifest


def setup(monkeypatch):
    def identity():
        return str(uuid.uuid4())

    scope = OwnerScope(identity(), identity(), identity(), 1)
    handle = identity()
    spec = ContainerSpec(scope, handle, "sha256:" + "a" * 64)
    native = ManagedContainer(scope, handle, "fixture-container", spec.image_id)
    lease = ResourceLease(handle, scope, "container", spec, native, "allocated")
    authority = FixtureAuthority(scope, {"task": lease})
    runtime = SimpleNamespace(client=SimpleNamespace(api=Mock()))
    provider = RootlessDockerProvider(runtime, authority, manifest())
    provider.owned.create = Mock(
        side_effect=lambda spec: ManagedExec(
            spec.container,
            spec.resource_id,
            "fixture-exec",
            OwnedExecRuntime._argv(spec),
            spec.control_key,
            spec.user,
        )
    )
    provider.owned.control = Mock(return_value={"stopped": True, "children": 0})
    process = SimpleNamespace(
        stdout=SimpleNamespace(read=AsyncMock(return_value=b"")),
        stderr=b"",
        stderr_truncated=False,
        wait=AsyncMock(return_value=0),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(OwnedDockerProcess, "start", AsyncMock(return_value=process))
    request = {
        "protocol_version": "1.0",
        "type": "broker_request",
        "id": identity(),
        "scope": {"workspace_id": scope.workspace_id, "trial_id": scope.trial_id, "generation": 1},
        "provider": "fixture.rootless",
        "operation": "exec",
        "handle": handle,
        "payload": {"argv": ["fixture-command"], "process_policy": "stop_after_command"},
    }
    return provider, authority, process, request


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code", [0, 17])
async def test_short_command_releases_before_reply_and_replay_does_not_repeat_effects(
    monkeypatch, exit_code
):
    provider, authority, process, request = setup(monkeypatch)
    process.wait.return_value = exit_code
    result = await provider.handle(request)
    assert result["state"] == "released" and result["exit_code"] == exit_code
    assert authority.resources[result["handle"]].state == "released"
    assert not provider._processes
    provider.owned.control.assert_called_once()
    assert provider.owned.control.call_args.args[1] == "release"
    process.aclose.assert_awaited_once()
    assert await provider.handle(request) == result
    provider.owned.create.assert_called_once()


@pytest.mark.asyncio
async def test_default_command_keeps_its_native_services_owned_until_cleanup(monkeypatch):
    provider, authority, process, request = setup(monkeypatch)
    request["payload"].pop("process_policy")
    result = await provider.handle(request)
    assert result["state"] == "retained"
    assert authority.resources[result["handle"]].state == "retained"
    provider.owned.control.assert_not_called()
    process.aclose.assert_not_awaited()
    assert provider._processes[result["handle"]] is process


@pytest.mark.asyncio
async def test_uncertain_release_never_publishes_success_or_drops_ownership(monkeypatch):
    provider, authority, process, request = setup(monkeypatch)
    provider.owned.control.side_effect = UncertainRuntimeError("fixture uncertain release")
    with pytest.raises(ProtocolError, match="reconciliation"):
        await provider.handle(request)
    assert [lease.state for lease in authority.resources.values() if lease.kind == "exec"] == [
        "releasing"
    ]
    assert provider._processes
    process.aclose.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy,mode", [("unsupported", "wait"), ([], "wait"), ("stop_after_command", "attach")]
)
async def test_invalid_policy_rejects_before_native_creation(monkeypatch, policy, mode):
    provider, _, _, request = setup(monkeypatch)
    request["payload"].update(process_policy=policy, mode=mode)
    with pytest.raises(ProtocolError, match="policy"):
        await provider.handle(request)
    provider.owned.create.assert_not_called()
