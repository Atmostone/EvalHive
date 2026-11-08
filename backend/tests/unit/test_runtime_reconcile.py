"""Recovery preserves uncertain effects and never repeats native startup."""

import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from app.benchmarks.recovery import recovery_order
from app.runtime.broker import ResourceLease
from app.runtime.lease_codec import encode_lease
from app.runtime.managed_exec import OwnedExecRuntime
from app.runtime.managed_spec import (
    ContainerSpec,
    ExecSpec,
    ManagedContainer,
    ManagedExec,
    ManagedNetwork,
    ManagedRuntimeError,
    NetworkRequest,
    OwnerScope,
)
from app.runtime.reconcile import RootlessReconciler
from app.runtime.records import resource_dependencies
from docker.errors import DockerException
from evalhive_benchmark import ProtocolError

from tests.runtime.authority_fixture import FixtureAuthority


def identity():
    return str(uuid.uuid4())


@pytest.fixture
def prepared():
    scope = OwnerScope(identity(), identity(), identity(), 1)
    handle = identity()
    spec = ContainerSpec(scope, handle, "sha256:" + "a" * 64)
    lease = ResourceLease(handle, scope, "container", spec, state="creating")
    native = ManagedContainer(scope, handle, "native", spec.image_id)
    authority, runtime = FixtureAuthority(scope, {"environment": lease}), MagicMock()
    runtime.find.return_value = native
    runtime.inspect.return_value = {"status": "running"}
    reconciler = RootlessReconciler(runtime, authority, "fixture.rootless")
    return reconciler, authority, runtime, lease, native


def request(lease, operation="inspect"):
    return {
        "protocol_version": "1.0",
        "type": "broker_request",
        "id": identity(),
        "scope": {
            "workspace_id": lease.scope.workspace_id,
            "trial_id": lease.scope.trial_id,
            "generation": lease.scope.generation,
        },
        "provider": "fixture.rootless",
        "operation": operation,
        "handle": lease.handle,
        "payload": {"reconcile": True},
    }


@pytest.mark.asyncio
async def test_creation_with_lost_reply_is_found_without_creating_or_starting(prepared):
    reconciler, authority, runtime, lease, native = prepared
    command = request(lease)
    assert (await reconciler.handle(command))["state"] == "allocated"
    assert authority.resources[lease.handle].native == native
    assert await reconciler.handle(command) == {
        "handle": lease.handle,
        "kind": "container",
        "state": "allocated",
    }
    runtime.find.assert_called_once_with(lease.scope, lease.intent.resource_id)
    runtime.create.assert_not_called()
    runtime.stop.assert_not_called()


@pytest.mark.asyncio
async def test_absent_unconfirmed_creation_remains_uncertain_until_later_observation(prepared):
    reconciler, authority, runtime, lease, native = prepared
    runtime.find.return_value = None
    with pytest.raises(ProtocolError) as missing:
        await reconciler.handle(request(lease, "release"))
    assert missing.value.kind == "uncertain"
    assert authority.resources[lease.handle].state == "creating"
    runtime.stop.assert_not_called()
    runtime.find.return_value = native
    runtime.inspect.side_effect = [{"status": "running"}, None]
    assert (await reconciler.handle(request(lease, "release")))["state"] == "released"
    assert authority.resources[lease.handle].state == "released"
    runtime.create.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_image_is_rejected_before_any_stop(prepared):
    reconciler, authority, runtime, lease, native = prepared
    runtime.find.return_value = replace(native, image_id="sha256:" + "b" * 64)
    with pytest.raises(ProtocolError, match="ownership"):
        await reconciler.handle(request(lease, "release"))
    assert authority.resources[lease.handle].state == "creating"
    runtime.stop.assert_not_called()


@pytest.mark.asyncio
async def test_lost_remove_reply_is_resolved_by_known_native_absence(prepared):
    reconciler, authority, runtime, lease, native = prepared
    authority.resources[lease.handle] = replace(lease, native=native, state="allocated")

    def remove(*args, **kwargs):
        assert authority.resources[lease.handle].state == "releasing"
        runtime.inspect.return_value = None
        raise DockerException("private native diagnostic")

    runtime.stop.side_effect = remove
    with pytest.raises(ProtocolError) as uncertain:
        await reconciler.handle(request(lease, "release"))
    assert uncertain.value.kind == "uncertain" and "private" not in str(uncertain.value)
    assert (await reconciler.handle(request(lease, "release")))["state"] == "released"
    runtime.stop.assert_called_once()


@pytest.mark.asyncio
async def test_never_allocated_plan_is_released_without_native_calls(prepared):
    reconciler, authority, runtime, lease, _ = prepared
    authority.resources[lease.handle] = replace(lease, state="planned")
    assert (await reconciler.handle(request(lease, "release")))["state"] == "released"
    runtime.find.assert_not_called()
    runtime.stop.assert_not_called()


def execution(prepared, *, state="running"):
    reconciler, authority, runtime, parent, native_parent = prepared
    authority.resources[parent.handle] = replace(parent, native=native_parent, state="allocated")
    handle = identity()
    spec = ExecSpec(native_parent, handle, ("fixture",))
    native = ManagedExec(
        native_parent, handle, "native-exec", OwnedExecRuntime._argv(spec), spec.control_key
    )
    lease = ResourceLease(handle, parent.scope, "exec", spec, native, state)
    authority.resources[handle] = lease
    reconciler.owned = MagicMock()
    reconciler.owned.control.return_value = {
        "main_done": True,
        "children": 1,
        "exit_code": 0,
        "stopped": False,
        "ok": True,
    }
    return lease


@pytest.mark.asyncio
async def test_completed_exec_keeps_its_background_services(prepared):
    reconciler, authority, runtime, _, _ = prepared
    lease = execution(prepared)
    result = await reconciler.handle(request(lease))
    assert result["state"] == "retained" and result["main_done"]
    reconciler.owned.control.assert_called_once_with(lease.native, "status")
    runtime.stop.assert_not_called()
    assert authority.resources[lease.handle].state == "retained"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["inspect", "stop", "release"])
async def test_missing_supervisor_marks_environment_unsafe_and_quiesces_only_on_control(
    prepared, operation
):
    reconciler, authority, runtime, parent, _ = prepared
    lease = execution(prepared)
    reconciler.owned.control.side_effect = ManagedRuntimeError("supervisor unavailable")
    runtime.inspect.side_effect = [{"status": "running"}, {"status": "exited"}]
    result = await reconciler.handle(request(lease, operation))
    assert result["environment_unsafe"]
    assert authority.resources[parent.handle].state == "unsafe"
    if operation == "inspect":
        runtime.stop.assert_not_called()
        assert authority.resources[lease.handle].state == "unsafe"
    else:
        runtime.stop.assert_called_once_with(lease.intent.container, remove=False)
        assert result["container_stopped"]
        assert authority.resources[lease.handle].state == (
            "released" if operation == "release" else "stopped"
        )


@pytest.mark.asyncio
async def test_unconfirmed_exec_cannot_run_in_stopped_parent(prepared):
    reconciler, authority, runtime, _, _ = prepared
    lease = execution(prepared, state="creating")
    authority.resources[lease.handle] = replace(lease, native=None)
    runtime.inspect.return_value = {"status": "exited"}
    assert (await reconciler.handle(request(lease, "release")))["state"] == "released"
    reconciler.owned.find.assert_not_called()
    reconciler.owned.control.assert_not_called()


@pytest.mark.asyncio
async def test_private_network_recovery_never_recreates_it(prepared):
    reconciler, authority, runtime, parent, _ = prepared
    handle = identity()
    lease = ResourceLease(
        handle, parent.scope, "network", NetworkRequest(parent.scope, handle), state="creating"
    )
    native = ManagedNetwork(parent.scope, handle, "native-network")
    authority.resources[handle] = lease
    runtime.find_network.return_value = native
    runtime.network.side_effect = [object(), None]
    assert (await reconciler.handle(request(lease, "release")))["state"] == "released"
    runtime.remove_network.assert_called_once_with(native)
    runtime.create_network.assert_not_called()


@pytest.mark.asyncio
async def test_early_deadline_request_performs_no_native_inspection(prepared):
    reconciler, authority, runtime, _, _ = prepared
    lease = execution(prepared)
    lease = replace(lease, deadline_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat())
    authority.resources[lease.handle] = lease
    command = request(lease, "stop")
    command["payload"] = {"expire": True}
    assert not (await reconciler.handle(command))["deadline_expired"]
    runtime.inspect.assert_not_called()
    reconciler.owned.control.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [True, False])
async def test_expiration_uses_atomic_native_condition_even_when_completion_races(
    prepared, stopped
):
    reconciler, authority, runtime, _, _ = prepared
    lease = execution(prepared)
    lease = replace(lease, deadline_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    authority.resources[lease.handle] = lease
    # The main process is active at inspection but may finish before the native
    # conditional operation. Host-side check-then-stop would kill its services.
    reconciler.owned.control.side_effect = [
        {"main_done": False, "children": 1, "exit_code": -1, "stopped": False, "ok": True},
        {
            "main_done": True,
            "children": 0 if stopped else 1,
            "exit_code": 137 if stopped else 0,
            "stopped": stopped,
            "ok": True,
        },
    ]
    command = request(lease, "stop")
    command["payload"] = {"expire": True}
    result = await reconciler.handle(command)
    assert result["deadline_expired"] and result["timed_out"] == stopped
    assert result["state"] == ("stopped" if stopped else "retained")
    assert [call.args[1] for call in reconciler.owned.control.call_args_list] == [
        "status",
        "expire",
    ]
    runtime.stop.assert_not_called()


def test_recovery_order_reverses_exec_namespace_and_network_dependencies(prepared):
    _, _, _, parent, native_parent = prepared
    network_id, child_id, exec_id = identity(), identity(), identity()
    network = ManagedNetwork(parent.scope, network_id, "private-network")
    parent = replace(parent, intent=replace(parent.intent, network=network))
    child_spec = replace(parent.intent, resource_id=child_id, network=None, namespace=native_parent)
    child_native = ManagedContainer(parent.scope, child_id, "child-native", parent.intent.image_id)
    child = ResourceLease(child_id, parent.scope, "container", child_spec)
    execute = ResourceLease(
        exec_id, parent.scope, "exec", ExecSpec(child_native, exec_id, ("fixture",))
    )
    net_lease = ResourceLease(
        network_id, parent.scope, "network", NetworkRequest(parent.scope, network_id)
    )
    leases = {parent.handle: parent, child_id: child, network_id: net_lease, exec_id: execute}
    dependencies = {
        handle: resource_dependencies(encode_lease(value), handle, value.scope)
        for handle, value in leases.items()
    }
    ordered, _ = recovery_order(dependencies)
    assert ordered == [exec_id, child_id, parent.handle, network_id]
    cyclic = replace(parent, intent=replace(parent.intent, network=None, namespace=child_native))
    with pytest.raises(ProtocolError, match="dependencies"):
        recovery_order(
            {
                value.handle: resource_dependencies(encode_lease(value), value.handle, value.scope)
                for value in [cyclic, child]
            }
        )


def test_recovery_metadata_does_not_interpret_provider_native_format(prepared):
    _, _, _, lease, _ = prepared
    dependency = identity()
    record = {
        "version": 1,
        "handle": lease.handle,
        "scope": asdict(lease.scope),
        "dependencies": [dependency],
        "intent": {"opaque_provider_setting": "fixture"},
        "native": {"future_provider_identity": "opaque"},
    }
    assert resource_dependencies(record, lease.handle, lease.scope) == {dependency}
    with pytest.raises(ManagedRuntimeError, match="metadata"):
        resource_dependencies(record, identity(), lease.scope)
    with pytest.raises(ManagedRuntimeError, match="migration"):
        resource_dependencies(
            {key: value for key, value in record.items() if key != "dependencies"},
            lease.handle,
            lease.scope,
        )
