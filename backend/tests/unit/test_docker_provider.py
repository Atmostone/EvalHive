"""Provider RPC consumes trusted grants and persists intents before native effects."""

import asyncio
import io
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.runtime.broker import ExportGrant, ResourceLease
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.managed_exec import OwnedExecRuntime
from app.runtime.managed_spec import (
    ContainerSpec,
    ManagedContainer,
    ManagedExec,
    ManagedNetwork,
    ManagedRuntimeError,
    NetworkRequest,
    OwnerScope,
    UncertainRuntimeError,
)
from app.runtime.reconcile import RootlessReconciler
from docker.errors import NotFound
from evalhive_benchmark import ProtocolError

from tests.runtime.authority_fixture import FixtureArtifacts, FixtureAuthority, manifest


def identity():
    return str(uuid.uuid4())


def request(scope, operation, payload=None, *, handle=None, key=None):
    value = {
        "protocol_version": "1.0",
        "type": "broker_request",
        "id": key or identity(),
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
        value["handle"] = handle
    return value


@pytest.fixture
def prepared():
    scope = OwnerScope(identity(), identity(), identity(), 1)
    spec = ContainerSpec(scope, identity(), "sha256:" + "a" * 64)
    lease = ResourceLease(identity(), scope, "container", spec)
    authority = FixtureAuthority(scope, {"task": lease})
    runtime = MagicMock()
    native = ManagedContainer(scope, spec.resource_id, "native-container", spec.image_id)
    runtime.create.return_value = native
    runtime.inspect.return_value = {"status": "running", "image_id": spec.image_id}
    artifacts = FixtureArtifacts()
    provider = RootlessDockerProvider(runtime, authority, manifest(), artifacts=artifacts)
    return provider, authority, runtime, lease, native, artifacts


async def allocate(prepared):
    provider, authority, _, lease, _, _ = prepared
    result = await provider.handle(request(authority.scope, "allocate", {"resource_id": "task"}))
    assert result["handle"] == lease.handle
    return authority.resources[lease.handle]


@pytest.mark.asyncio
async def test_allocation_commits_intent_before_effect_and_receipt_after_it(prepared):
    provider, authority, runtime, lease, native, _ = prepared

    def create(spec):
        assert authority.resources[lease.handle].state == "creating"
        authority.journal.append(("effect", lease.handle, None))
        return native

    runtime.create.side_effect = create
    command = request(authority.scope, "allocate", {"resource_id": "task"}, key="stable-allocation")
    result = await provider.handle(command)
    assert authority.journal == [
        ("save", lease.handle, "creating"),
        ("effect", lease.handle, None),
        ("save", lease.handle, "allocated"),
        ("receipt", "stable-allocation", None),
    ]
    assert await provider.handle(command) == result
    runtime.create.assert_called_once()
    # A new request for the same immutable plan resource also reuses it safely.
    assert (
        await provider.handle(request(authority.scope, "allocate", {"resource_id": "task"}))
        == result
    )
    runtime.create.assert_called_once()
    with pytest.raises(ProtocolError) as caught:
        await provider.handle({**command, "payload": {"resource_id": "different"}})
    assert caught.value.code == "COMMAND_CONFLICT"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"resource_id": "task", "image": "arbitrary"},
        {"resource_id": "task", "host_path": "/run/docker.sock"},
        {"resource_id": "missing"},
        {"resource_id": "/host/path"},
        {"resource_id": True},
    ],
)
async def test_worker_cannot_select_unregistered_images_or_paths(prepared, payload):
    provider, authority, runtime, _, _, _ = prepared
    with pytest.raises(ProtocolError):
        await provider.handle(request(authority.scope, "allocate", payload))
    runtime.create.assert_not_called()


@pytest.mark.asyncio
async def test_missing_reservation_and_stale_generation_cannot_create(prepared):
    provider, authority, runtime, _, _, _ = prepared
    command = request(authority.scope, "allocate", {"resource_id": "task"})
    authority.reserved = False
    with pytest.raises(ProtocolError) as caught:
        await provider.handle(command)
    assert caught.value.code == "NO_RESERVATION"
    authority.reserved = True
    command["scope"]["generation"] = 2
    with pytest.raises(ProtocolError) as caught:
        await provider.handle(command)
    assert caught.value.code == "STALE_GENERATION"
    runtime.create.assert_not_called()


@pytest.mark.asyncio
async def test_uncertain_creation_keeps_intent_and_never_repeats_effect(prepared):
    provider, authority, runtime, lease, _, _ = prepared
    runtime.create.side_effect = UncertainRuntimeError("private native diagnostic")
    command = request(authority.scope, "allocate", {"resource_id": "task"}, key="uncertain")
    for _ in range(2):
        with pytest.raises(ProtocolError) as caught:
            await provider.handle(command)
        assert caught.value.code == "RESOURCE_UNCERTAIN" and "private" not in str(caught.value)
    assert authority.resources[lease.handle].state == "creating"
    runtime.create.assert_called_once()
    with pytest.raises(ProtocolError) as caught:
        await provider.handle(request(authority.scope, "release", handle=lease.handle))
    assert caught.value.code == "RESOURCE_UNCERTAIN"
    runtime.stop.assert_not_called()


@pytest.mark.asyncio
async def test_foreign_handle_scope_or_native_identity_is_rejected(prepared):
    provider, authority, runtime, lease, native, _ = prepared
    await allocate(prepared)
    for wrong in [
        replace(lease, scope=replace(lease.scope, generation=2)),
        replace(lease, native=replace(native, resource_id=identity())),
        replace(lease, native=replace(native, image_id="sha256:" + "b" * 64)),
    ]:
        authority.resources[lease.handle] = wrong
        with pytest.raises(ProtocolError):
            await provider.handle(request(authority.scope, "release", handle=lease.handle))
    runtime.stop.assert_not_called()


@pytest.mark.asyncio
async def test_native_network_uses_reserved_scope_and_egress(prepared):
    provider, authority, runtime, _, _, _ = prepared
    spec = NetworkRequest(authority.scope, identity(), False)
    lease = ResourceLease(identity(), authority.scope, "network", spec)
    authority.plans["network"] = lease
    authority.resources[lease.handle] = lease
    runtime.create_network.return_value = ManagedNetwork(
        spec.scope, spec.resource_id, "private-network"
    )
    await provider.handle(request(authority.scope, "allocate", {"resource_id": "network"}))
    runtime.create_network.assert_called_once_with(spec.scope, spec.resource_id, egress=False)
    with pytest.raises(ProtocolError) as caught:
        await provider.handle(request(authority.scope, "stop", handle=lease.handle))
    assert caught.value.code == "UNSUPPORTED_CAPABILITY"
    assert (await provider.handle(request(authority.scope, "release", handle=lease.handle)))[
        "released"
    ]
    runtime.remove_network.assert_called_once()


def fake_process(data=b"native-output", *, code=0):
    return SimpleNamespace(
        environment_broken=False,
        stdout=SimpleNamespace(read=AsyncMock(side_effect=[data, b""])),
        stderr=bytearray(b"native-diagnostic"),
        stderr_truncated=False,
        wait=AsyncMock(return_value=code),
        aclose=AsyncMock(),
    )


def native_exec(spec):
    return ManagedExec(
        spec.container,
        spec.resource_id,
        "native-exec",
        OwnedExecRuntime._argv(spec),
        spec.control_key,
        spec.user,
    )


@pytest.mark.asyncio
async def test_exec_persists_private_control_before_start_and_retains_lease(prepared, monkeypatch):
    provider, authority, _, _, _, _ = prepared
    container = await allocate(prepared)
    authority.private_inputs["fixture-config"] = b"private-fixture-value"
    provider.owned = MagicMock()
    provider.owned.create.side_effect = native_exec
    process = fake_process(b"x" * 100000)

    async def start(owned, handle, spec, **callbacks):
        saved = authority.resources[spec.resource_id]
        assert saved.state == "created" and saved.native == handle
        assert saved.intent.control_key == spec.control_key
        return process

    monkeypatch.setattr("app.runtime.docker_provider.OwnedDockerProcess.start", start)
    command = request(
        authority.scope,
        "exec",
        {
            "argv": ["native-command"],
            "timeout_seconds": 1,
            "private_input_refs": {"CONFIG": "fixture-config"},
        },
        handle=container.handle,
    )
    result = await provider.handle(command)
    assert (
        result["exit_code"] == 0 and len(result["stdout"]) == 65536 and result["stdout_truncated"]
    )
    assert "private-fixture-value" not in repr(result)
    assert authority.resources[result["handle"]].state == "retained"
    provider.owned.control.assert_not_called()  # Native completion preserves background services.
    assert await provider.handle(command) == result
    provider.owned.create.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("inherit", [False, True])
async def test_host_may_shorten_only_explicit_inherited_budgets_and_reports_effective_timeout(
    prepared, monkeypatch, inherit
):
    from tests.runtime.authority_fixture import FixtureCall

    provider, authority, _, _, _, _ = prepared
    container = await allocate(prepared)
    original = FixtureCall.execution

    async def shortened(call, target, payload):
        grant = await original(call, target, payload)
        return replace(grant, timeout_seconds=0.5)

    monkeypatch.setattr(FixtureCall, "execution", shortened)
    provider.owned = MagicMock()
    provider.owned.create.side_effect = native_exec

    async def start(*args, **options):
        return fake_process()

    monkeypatch.setattr("app.runtime.docker_provider.OwnedDockerProcess.start", start)
    payload = {"argv": ["native-command"], "timeout_seconds": 1}
    if inherit:
        payload["deadline_policy"] = "inherit"
        result = await provider.handle(
            request(authority.scope, "exec", payload, handle=container.handle)
        )
        assert result["exit_code"] == 0 and result["effective_timeout_seconds"] == 0.5
    else:
        with pytest.raises(ProtocolError):
            await provider.handle(
                request(authority.scope, "exec", payload, handle=container.handle)
            )
        provider.owned.create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"argv": []},
        {"argv": ["bad\0command"]},
        {"argv": ["command"], "mode": "restart"},
        {"argv": ["command"], "deadline_policy": []},
        {"argv": ["command"], "deadline_policy": "unknown"},
        {"argv": ["command"], "timeout_seconds": True},
        {"argv": ["command"], "timeout_seconds": 31},
        {"argv": ["command"], "private_input_refs": {"CONFIG": "foreign"}},
        {"argv": ["command"], "environment": {"BAD=NAME": "value"}},
        {"argv": ["command"], "cwd": "/tmp/../root"},
    ],
)
async def test_invalid_exec_settings_fail_before_native_create(prepared, payload):
    provider, authority, _, _, _, _ = prepared
    container = await allocate(prepared)
    provider.owned = MagicMock()
    with pytest.raises(ProtocolError):
        await provider.handle(request(authority.scope, "exec", payload, handle=container.handle))
    provider.owned.create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
async def test_exec_deadline_stops_tree_and_preserves_lease_for_cleanup(
    prepared, monkeypatch, completed
):
    provider, authority, _, _, _, _ = prepared
    container = await allocate(prepared)
    provider.owned = MagicMock()
    provider.owned.create.side_effect = native_exec
    provider.owned.control.return_value = {
        "stopped": not completed,
        "main_done": completed,
        "exit_code": 0 if completed else None,
    }
    process = fake_process()

    async def blocked_read():
        await asyncio.sleep(10)

    process.stdout.read = blocked_read
    monkeypatch.setattr(
        "app.runtime.docker_provider.OwnedDockerProcess.start", AsyncMock(return_value=process)
    )
    with pytest.raises(ProtocolError) as caught:
        await provider.handle(
            request(
                authority.scope,
                "exec",
                {"argv": ["native-command"], "timeout_seconds": 0.02},
                handle=container.handle,
            )
        )
    assert caught.value.code == "RESOURCE_TIMEOUT"
    provider.owned.control.assert_called_once()
    assert provider.owned.control.call_args.args[1] == "expire"
    assert next(value for value in authority.resources.values() if value.kind == "exec").state == (
        "retained" if completed else "stopped"
    )


@pytest.mark.asyncio
async def test_export_uses_saved_output_grant_and_complete_bounded_stream(prepared):
    provider, authority, runtime, lease, _, artifacts = prepared
    await allocate(prepared)
    grant = ExportGrant("result", "/workspace/result.txt", "user", 1000)
    authority.outputs[lease.handle, "result"] = grant
    stream = io.BytesIO(b"native-tar-data")
    runtime.container.return_value.get_archive.return_value = (iter([stream.read()]), {})
    result = await provider.handle(
        request(authority.scope, "export", {"output_id": "result"}, handle=lease.handle)
    )
    assert result["artifact"]["id"] == "result" and artifacts.values["result"] == b"native-tar-data"
    runtime.container.return_value.get_archive.assert_called_once_with(grant.path)
    with pytest.raises(ProtocolError):
        await provider.handle(
            request(authority.scope, "export", {"output_id": "/etc/passwd"}, handle=lease.handle)
        )


@pytest.mark.asyncio
async def test_missing_oversized_or_unconsumed_evidence_cannot_succeed(prepared):
    provider, authority, runtime, lease, _, _ = prepared
    await allocate(prepared)
    authority.outputs[lease.handle, "result"] = ExportGrant(
        "result", "/workspace/result", "user", 3
    )
    native = runtime.container.return_value
    native.get_archive.side_effect = NotFound("private path detail")
    with pytest.raises(ProtocolError) as caught:
        await provider.handle(
            request(authority.scope, "export", {"output_id": "result"}, handle=lease.handle)
        )
    assert caught.value.code == "ARTIFACT_MISSING"
    native.get_archive.side_effect = None
    native.get_archive.return_value = (iter([b"too large"]), {})
    with pytest.raises(ProtocolError) as caught:
        await provider.handle(
            request(authority.scope, "export", {"output_id": "result"}, handle=lease.handle)
        )
    assert caught.value.code == "ARTIFACT_TOO_LARGE"
    provider.artifacts = SimpleNamespace(save=AsyncMock(return_value={}))
    native.get_archive.return_value = (iter([b"ok"]), {})
    with pytest.raises(ProtocolError) as caught:
        await provider.handle(
            request(authority.scope, "export", {"output_id": "result"}, handle=lease.handle)
        )
    assert caught.value.code == "ARTIFACT_INCOMPLETE"


@pytest.mark.asyncio
@pytest.mark.parametrize("path,rootfs", [("/", True), ("/", False), ("//", False)])
async def test_full_rootfs_is_private_and_cannot_be_a_public_artifact(prepared, path, rootfs):
    provider, authority, runtime, lease, _, _ = prepared
    await allocate(prepared)
    authority.outputs[lease.handle, "snapshot"] = ExportGrant(
        "snapshot", path, "user", 1000, rootfs
    )
    with pytest.raises(ProtocolError):
        await provider.handle(
            request(authority.scope, "export", {"output_id": "snapshot"}, handle=lease.handle)
        )
    runtime.container.return_value.export.assert_not_called()
    runtime.container.return_value.get_archive.assert_not_called()


@pytest.mark.asyncio
async def test_released_resource_cleanup_is_idempotent(prepared):
    provider, authority, runtime, lease, _, _ = prepared
    await allocate(prepared)
    for _ in range(2):
        assert (await provider.handle(request(authority.scope, "release", handle=lease.handle)))[
            "released"
        ]
    runtime.stop.assert_called_once()


@pytest.mark.asyncio
async def test_transport_controls_recheck_generation_and_cannot_stop_a_new_owner(
    prepared, monkeypatch
):
    provider, authority, runtime, _, _, _ = prepared
    container = await allocate(prepared)
    provider.owned = MagicMock()
    provider.owned.create.side_effect = native_exec
    callbacks = {}

    async def start(owned, handle, spec, **hooks):
        callbacks.update(hooks)
        process = fake_process()
        process.handle = handle
        return process

    monkeypatch.setattr("app.runtime.docker_provider.OwnedDockerProcess.start", start)
    result = await provider.handle(
        request(
            authority.scope, "exec", {"argv": ["worker"], "mode": "attach"}, handle=container.handle
        )
    )
    assert authority.resources[result["handle"]].deadline_at
    authority.scope = replace(authority.scope, generation=2)
    with pytest.raises(ProtocolError) as caught:
        await callbacks["control"]("stop")
    assert caught.value.code == "STALE_GENERATION"
    with pytest.raises(ProtocolError) as caught:
        await callbacks["on_broken"]()
    assert caught.value.code == "STALE_GENERATION"
    provider.owned.control.assert_not_called()
    runtime.stop.assert_not_called()


@pytest.mark.asyncio
async def test_lost_native_supervisor_is_recording_failure_and_marks_environment_unsafe(
    prepared, monkeypatch
):
    from app.runtime.managed_spec import ManagedRuntimeError

    provider, authority, _, _, _, _ = prepared
    container = await allocate(prepared)
    provider.owned = MagicMock()
    provider.owned.create.side_effect = native_exec
    process = fake_process()
    process.environment_broken = True
    process.stdout.read.side_effect = ManagedRuntimeError("Safe transport diagnostic")
    monkeypatch.setattr(
        "app.runtime.docker_provider.OwnedDockerProcess.start", AsyncMock(return_value=process)
    )
    with pytest.raises(ProtocolError) as caught:
        await provider.handle(
            request(authority.scope, "exec", {"argv": ["worker"]}, handle=container.handle)
        )
    assert caught.value.code == "ENVIRONMENT_UNSAFE" and caught.value.kind == "recording"
    assert authority.resources[container.handle].state == "unsafe"


@pytest.mark.asyncio
async def test_container_transport_is_host_only_and_never_reattached(prepared, monkeypatch):
    provider, authority, _, lease, native, _ = prepared
    registered = replace(lease, intent=replace(lease.intent, role="worker"))
    authority.plans["task"] = registered
    authority.resources[lease.handle] = registered
    await allocate(prepared)
    process = SimpleNamespace(handle=native)
    attach = AsyncMock(return_value=process)
    monkeypatch.setattr("app.runtime.docker_provider.ManagedDockerProcess.attach", attach)
    command = request(authority.scope, "inspect", handle=lease.handle)
    assert (await provider.handle(command))["kind"] == "container"
    assert not attach.called
    value = await provider.open_transport(request(authority.scope, "inspect", handle=lease.handle))
    assert value is process
    with pytest.raises(ValueError, match="already attached"):
        await provider.open_transport(request(authority.scope, "inspect", handle=lease.handle))
    attach.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["container", "network"])
async def test_known_preflight_rejection_releases_without_native_retry(prepared, kind):
    provider, authority, runtime, lease, _, _ = prepared
    if kind == "network":
        spec = NetworkRequest(lease.scope, identity())
        lease = ResourceLease(identity(), lease.scope, "network", spec)
        authority.plans["task"] = lease
        authority.resources[lease.handle] = lease
    mutation = runtime.create if kind == "container" else runtime.create_network
    mutation.side_effect = ManagedRuntimeError("private validation diagnostic")
    command = request(authority.scope, "allocate", {"resource_id": "task"})
    with pytest.raises(ProtocolError) as rejected:
        await provider.handle(command)
    assert rejected.value.code == "RESOURCE_REJECTED"
    assert "private" not in str(rejected.value)
    assert authority.resources[lease.handle].state == "rejected"
    assert authority.resources[lease.handle].native is None
    await provider.handle(request(authority.scope, "release", handle=lease.handle))
    assert authority.resources[lease.handle].state == "released"
    mutation.assert_called_once()
    runtime.stop.assert_not_called()
    runtime.remove_network.assert_not_called()


@pytest.mark.asyncio
async def test_rejected_exec_cleanup_does_not_quiesce_its_parent(prepared):
    provider, authority, runtime, _, _, _ = prepared
    parent = await allocate(prepared)
    provider.owned = MagicMock()
    provider.owned.create.side_effect = ManagedRuntimeError("owned bundle validation failed")
    with pytest.raises(ProtocolError) as rejected:
        await provider.handle(
            request(authority.scope, "exec", {"argv": ["fixture"]}, handle=parent.handle)
        )
    assert rejected.value.code == "RESOURCE_REJECTED"
    lease = next(value for value in authority.resources.values() if value.kind == "exec")
    assert lease.state == "rejected" and lease.native is None
    recovery = RootlessReconciler(runtime, authority, provider.manifest["id"])
    await recovery.handle(
        request(authority.scope, "stop", {"reconcile": True}, handle=lease.handle)
    )
    assert authority.resources[lease.handle].state == "unallocated"
    await recovery.handle(
        request(authority.scope, "release", {"reconcile": True}, handle=lease.handle)
    )
    assert authority.resources[lease.handle].state == "released"
    assert authority.resources[parent.handle].state == "allocated"
    runtime.stop.assert_not_called()
    recovery.owned.api.exec_inspect.assert_not_called()


@pytest.mark.asyncio
async def test_stop_of_never_allocated_resource_does_not_lose_no_effect_proof(prepared):
    provider, authority, runtime, lease, _, _ = prepared
    await provider.handle(request(authority.scope, "stop", handle=lease.handle))
    assert authority.resources[lease.handle].state == "unallocated"
    await provider.handle(request(authority.scope, "release", handle=lease.handle))
    assert authority.resources[lease.handle].state == "released"
    runtime.create.assert_not_called()
    runtime.stop.assert_not_called()


@pytest.mark.asyncio
async def test_uncertain_exec_create_is_not_marked_as_no_effect(prepared):
    provider, authority, _, _, _, _ = prepared
    parent = await allocate(prepared)
    provider.owned = MagicMock()
    provider.owned.create.side_effect = UncertainRuntimeError("lost reply")
    with pytest.raises(ProtocolError) as uncertain:
        await provider.handle(
            request(authority.scope, "exec", {"argv": ["fixture"]}, handle=parent.handle)
        )
    assert uncertain.value.kind == "uncertain"
    lease = next(value for value in authority.resources.values() if value.kind == "exec")
    assert lease.state == "creating" and lease.native is None
    with pytest.raises(ProtocolError) as pending:
        await provider.handle(request(authority.scope, "release", handle=lease.handle))
    assert pending.value.kind == "uncertain"
