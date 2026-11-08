"""Neutral runtime ownership, native startup and bounded server-controlled mounts."""

import socket
import uuid
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.runtime.managed_docker import RootlessDockerRuntime
from app.runtime.managed_spec import (
    ContainerSpec,
    ManagedContainer,
    ManagedNetwork,
    ManagedRuntimeError,
    OwnerScope,
    PathMapping,
    Quota,
    RuntimePolicy,
    UncertainRuntimeError,
)
from app.runtime.spec import BindMount
from docker.errors import APIError, NotFound


def identity():
    return str(uuid.uuid4())


@pytest.fixture
def configured(tmp_path):
    scope = OwnerScope(identity(), identity(), identity(), 3)
    local, bundle = tmp_path / "local", tmp_path / "bundles"
    (local / "benchmark-sessions" / scope.session_id).mkdir(parents=True)
    (bundle / "installed").mkdir(parents=True)
    policy = RuntimePolicy(
        PathMapping("/host/data", str(local)), (PathMapping("/host/bundles", str(bundle)),)
    )
    client = MagicMock()
    client.info.return_value = {"SecurityOptions": ["name=rootless"]}
    image = "sha256:" + "a" * 64
    client.images.get.return_value = SimpleNamespace(
        id=image, attrs={"Os": "linux", "Architecture": "amd64"}
    )
    spec = ContainerSpec(scope, identity(), image)
    container = MagicMock()
    container.id, container.status = "container", "running"
    container.labels = {
        **scope.labels(),
        "evalhive.resource_id": spec.resource_id,
        "evalhive.role": "benchmark.environment",
    }
    container.attrs = {
        "Image": image,
        "Config": {"Labels": container.labels},
        "State": {"Status": "running", "ExitCode": 0},
    }
    client.containers.create.return_value = container
    client.containers.get.return_value = container
    return RootlessDockerRuntime(client, policy), client, spec, container


def test_native_image_startup_and_workspace_are_preserved(configured):
    runtime, client, spec, container = configured
    result = runtime.create(spec)
    assert result.container_id == container.id
    kwargs = client.containers.create.call_args.kwargs
    assert (
        kwargs["entrypoint"] is kwargs["command"] is kwargs["working_dir"] is kwargs["user"] is None
    )
    assert kwargs["volumes"] == {}  # No empty workspace hides native image files.
    assert kwargs["network_disabled"] and kwargs["mem_limit"] == "2048m"
    assert kwargs["cpu_quota"] == 100000 and kwargs["pids_limit"] == 256
    assert kwargs["cap_drop"] == ["ALL"] and kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["labels"]["evalhive.generation"] == "3"
    container.start.assert_called_once()


@pytest.mark.parametrize("field", ["workspace_id", "trial_id", "session_id", "generation"])
def test_foreign_scope_cannot_stop_or_inspect_a_resource(configured, field):
    runtime, client, spec, container = configured
    foreign = replace(spec.scope, **{field: 9 if field == "generation" else identity()})
    handle = ManagedContainer(foreign, spec.resource_id, container.id, spec.image_id)
    for operation in (runtime.inspect, runtime.stop):
        with pytest.raises(ManagedRuntimeError, match="does not belong"):
            operation(handle)
    container.stop.assert_not_called()


def test_handles_bind_resource_identity_and_image(configured):
    runtime, client, spec, container = configured
    for resource, image in [(identity(), spec.image_id), (spec.resource_id, "sha256:" + "b" * 64)]:
        with pytest.raises(ManagedRuntimeError, match="does not belong"):
            runtime.stop(ManagedContainer(spec.scope, resource, container.id, image), remove=True)
    container.remove.assert_not_called()


@pytest.mark.parametrize(
    "quota",
    [
        Quota(memory_mb=4096),
        Quota(cpu_cores=2),
        Quota(pids=1025),
        Quota(cpu_cores=float("nan")),
        Quota(memory_mb=True),
        Quota(cpu_cores=True),
    ],
)
def test_native_limits_are_rejected_instead_of_lowered(configured, quota):
    runtime, client, spec, _ = configured
    with pytest.raises(ManagedRuntimeError, match="requirements exceed"):
        runtime.create(replace(spec, quota=quota))
    client.containers.create.assert_not_called()


def test_rootful_daemon_and_foreign_platform_fail_before_creation(configured):
    runtime, client, spec, _ = configured
    client.info.return_value = {"SecurityOptions": ["name=seccomp"]}
    with pytest.raises(ManagedRuntimeError, match="rootless"):
        runtime.create(spec)
    client.info.return_value = {"SecurityOptions": ["name=rootless"]}
    client.images.get.return_value.attrs["Architecture"] = "arm64"
    with pytest.raises(ManagedRuntimeError, match="unsupported platform"):
        runtime.create(spec)
    client.containers.create.assert_not_called()


def test_host_local_mapping_and_readonly_bundle_registration(configured):
    runtime, client, spec, _ = configured
    scope_dir = f"/host/data/benchmark-sessions/{spec.scope.session_id}"
    runtime.create(
        replace(
            spec,
            mounts=(
                BindMount(scope_dir, "/work", False),
                BindMount("/host/bundles/installed", "/opt/runtime"),
            ),
        )
    )
    volumes = client.containers.create.call_args.kwargs["volumes"]
    assert volumes[scope_dir]["mode"] == "rw"
    assert volumes["/host/bundles/installed"]["mode"] == "ro"
    for mounts in [
        (BindMount("/host/bundles/installed", "/opt/runtime", False),),
        (BindMount("/etc", "/input"),),
        (BindMount(scope_dir, "/work"), BindMount("/host/bundles/installed", "/work/nested")),
    ]:
        with pytest.raises(ManagedRuntimeError):
            runtime.create(replace(spec, mounts=mounts))


def test_symlink_and_socket_mount_sources_are_rejected(configured, tmp_path):
    runtime, _, spec, _ = configured
    local = runtime.policy.data.local + "/benchmark-sessions/" + spec.scope.session_id
    (Path(local) / "link").symlink_to(tmp_path)
    with pytest.raises(ManagedRuntimeError, match="links or expose sockets"):
        runtime.create(
            replace(
                spec,
                mounts=(
                    BindMount(
                        f"/host/data/benchmark-sessions/{spec.scope.session_id}/link", "/input"
                    ),
                ),
            )
        )
    with TemporaryDirectory(prefix="eh-") as directory:
        runtime.policy = replace(
            runtime.policy,
            readonly=(*runtime.policy.readonly, PathMapping("/host/socket", directory)),
        )
        connection = socket.socket(socket.AF_UNIX)
        try:
            connection.bind(directory + "/socket")
            with pytest.raises(ManagedRuntimeError, match="links or expose sockets"):
                runtime.create(replace(spec, mounts=(BindMount("/host/socket/socket", "/input"),)))
        finally:
            connection.close()


def test_network_and_namespace_are_fenced_and_not_global(configured):
    runtime, client, spec, container = configured
    network_id = identity()
    labels = {**spec.scope.labels(), "evalhive.resource_id": network_id}
    network = MagicMock()
    network.id, network.attrs = "network", {"Labels": labels}
    client.networks.create.return_value = network
    client.networks.get.return_value = network
    handle = runtime.create_network(spec.scope, network_id)
    assert client.networks.create.call_args.kwargs["internal"] is True
    runtime.create(replace(spec, network=handle))
    assert client.containers.create.call_args.kwargs["network"] == "network"
    foreign = replace(handle, scope=replace(spec.scope, session_id=identity()))
    with pytest.raises(ManagedRuntimeError, match="does not belong"):
        runtime.create(replace(spec, network=foreign))
    parent = ManagedContainer(spec.scope, spec.resource_id, container.id, spec.image_id)
    runtime.create(replace(spec, namespace=parent))
    assert client.containers.create.call_args.kwargs["network_mode"] == "container:container"
    container.labels["evalhive.role"] = "benchmark.worker"
    with pytest.raises(ManagedRuntimeError, match="not a task environment"):
        runtime.create(replace(spec, namespace=parent))


def test_failed_creation_requires_reconciliation_and_diagnostics_are_private(configured):
    runtime, client, spec, container = configured
    client.containers.create.side_effect = APIError("private environment data")
    with pytest.raises(UncertainRuntimeError) as caught:
        runtime.create(replace(spec, environment={"PRIVATE_TOKEN": "private token value"}))
    assert "private" not in str(caught.value)
    assert "private token value" not in repr(
        replace(spec, environment={"PRIVATE_TOKEN": "private token value"})
    )
    client.containers.list.return_value = [container]
    assert runtime.find(spec.scope, spec.resource_id).container_id == container.id
    client.containers.list.return_value = [container, container]
    with pytest.raises(ManagedRuntimeError, match="conflicting"):
        runtime.find(spec.scope, spec.resource_id)


def test_release_checks_authority_and_does_not_return_private_configuration(configured):
    runtime, client, spec, container = configured
    handle = ManagedContainer(spec.scope, spec.resource_id, container.id, spec.image_id)
    container.attrs["Config"]["Env"] = ["PRIVATE=value"]
    assert "PRIVATE" not in str(runtime.inspect(handle))
    assert runtime.stop(handle, remove=True)
    container.remove.assert_called_once_with(force=True, v=True)
    client.containers.get.side_effect = NotFound("absent")
    assert runtime.stop(handle) is False
    assert runtime.inspect(handle) is None


def test_invalid_owner_generation_and_unpinned_image_fail_closed(configured):
    runtime, client, spec, _ = configured
    with pytest.raises(ManagedRuntimeError):
        replace(spec.scope, generation=True)
    with pytest.raises(ManagedRuntimeError):
        OwnerScope("other", identity(), identity(), 1)
    with pytest.raises(ManagedRuntimeError, match="immutable"):
        runtime.create(replace(spec, image_id="example:latest"))
    client.containers.create.assert_not_called()


def test_service_aliases_are_bound_to_the_owned_private_network(configured):
    runtime, client, spec, _ = configured
    network = ManagedNetwork(spec.scope, identity(), "private-network")
    runtime.network = MagicMock(return_value=object())
    client.api.create_endpoint_config.return_value = {"Aliases": ["database", "database.local"]}
    runtime.create(replace(spec, network=network, network_aliases=("database", "database.local")))
    kwargs = client.containers.create.call_args.kwargs
    assert kwargs["network"] == "private-network"
    assert kwargs["networking_config"] == {
        "private-network": {"Aliases": ["database", "database.local"]}
    }
    with pytest.raises(ManagedRuntimeError, match="aliases"):
        runtime.create(replace(spec, network_aliases=("database",)))
    with pytest.raises(ManagedRuntimeError, match="aliases"):
        runtime.create(replace(spec, network=network, network_aliases=("duplicate", "duplicate")))
    with pytest.raises(ManagedRuntimeError, match="aliases"):
        runtime.create(replace(spec, network=network, network_aliases=("/host/path",)))
