"""Owned storage has no host path authority and cannot cross environment epochs."""

import copy
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.runtime.managed_spec import (
    ContainerSpec,
    ManagedRuntimeError,
    OwnerScope,
    UncertainRuntimeError,
    VolumeMount,
    VolumeRequest,
)
from app.runtime.managed_storage import RootlessVolumeRuntime
from app.runtime.spec import BindMount
from docker.errors import APIError, NotFound


def scope():
    return OwnerScope(*(str(uuid.uuid4()) for _ in range(3)), 1)


class Volumes:
    def __init__(self):
        self.values, self.live, self.fail_after_create = {}, set(), False

    def get(self, name):
        if name not in self.values:
            raise NotFound("Missing fixture storage")
        return self.values[name]

    def create(self, *, name, driver, labels):
        def remove(*, force):
            assert force is False
            if name in self.live:
                raise APIError("Fixture storage is still mounted")
            del self.values[name]

        value = SimpleNamespace(
            attrs={
                "Name": name,
                "Driver": driver,
                "Scope": "local",
                "Labels": labels,
                "Options": None,
            },
            remove=remove,
        )
        self.values[name] = value
        if self.fail_after_create:
            raise APIError("Lost fixture create reply")
        return value


def runtime():
    volumes = Volumes()
    return RootlessVolumeRuntime(
        SimpleNamespace(client=SimpleNamespace(volumes=volumes), check_daemon=lambda: None)
    ), volumes


def test_volume_creation_loss_reconciles_original_identity_without_repeating_startup():
    storage, native = runtime()
    request = VolumeRequest(scope(), str(uuid.uuid4()), 64)
    native.fail_after_create = True
    with pytest.raises(UncertainRuntimeError):
        storage.create(request)
    handle = storage.find(request.scope, request.resource_id)
    assert handle is not None and handle.scope == request.scope
    with pytest.raises(ManagedRuntimeError):
        storage.create(request)
    assert len(native.values) == 1
    storage.remove(handle)
    assert storage.get(handle) is None


@pytest.mark.parametrize(
    "mutation",
    [
        {"Driver": "external"},
        {"Scope": "global"},
        {"Options": {"device": "/private"}},
        {"Labels": {}},
    ],
)
def test_foreign_driver_options_or_labels_cannot_be_reused_or_deleted(mutation):
    storage, native = runtime()
    request = VolumeRequest(scope(), str(uuid.uuid4()), 64)
    handle = storage.create(request)
    native.values[handle.name].attrs.update(copy.deepcopy(mutation))
    with pytest.raises(ManagedRuntimeError):
        storage.get(handle)
    with pytest.raises(ManagedRuntimeError):
        storage.remove(handle)
    assert handle.name in native.values


def test_volume_release_requires_actual_docker_detachment():
    storage, native = runtime()
    handle = storage.create(VolumeRequest(scope(), str(uuid.uuid4()), 64))
    native.live.add(handle.name)
    with pytest.raises(UncertainRuntimeError):
        storage.remove(handle)
    assert storage.get(handle) == handle
    native.live.clear()
    storage.remove(handle)
    storage.remove(handle)


def test_storage_and_container_mounts_survive_private_lease_serialization():
    from app.runtime.broker import ResourceLease
    from app.runtime.lease_codec import decode_lease, encode_lease

    storage, _ = runtime()
    request = VolumeRequest(scope(), str(uuid.uuid4()), 64)
    handle = storage.create(request)
    lease = ResourceLease(
        request.resource_id, request.scope, "volume", request, handle, "allocated"
    )
    assert decode_lease(encode_lease(lease)) == lease
    spec = ContainerSpec(
        request.scope,
        str(uuid.uuid4()),
        "sha256:" + "a" * 64,
        storage=(VolumeMount(handle, "/workspace", True),),
    )
    lease = ResourceLease(spec.resource_id, request.scope, "container", spec)
    assert decode_lease(encode_lease(lease)) == lease
    legacy = encode_lease(replace(lease, intent=replace(spec, storage=())))
    del legacy["intent"]["storage"]
    assert decode_lease(legacy).intent.storage == ()


def test_mounts_require_same_session_and_distinct_contained_registered_targets():
    storage, _ = runtime()
    owner = scope()
    handle = storage.create(VolumeRequest(owner, str(uuid.uuid4()), 64))
    spec = ContainerSpec(
        owner, str(uuid.uuid4()), "sha256:" + "a" * 64, storage=(VolumeMount(handle, "/workspace"),)
    )
    assert storage.bindings(spec) == {handle.name: {"bind": "/workspace", "mode": "rw"}}
    with pytest.raises(ManagedRuntimeError):
        storage.bindings(replace(spec, scope=replace(owner, generation=2)))
    with pytest.raises(ManagedRuntimeError):
        storage.bindings(
            replace(spec, mounts=(BindMount("/owned/source", "/workspace/hidden", True),))
        )
    with pytest.raises(ManagedRuntimeError):
        VolumeMount(handle, "/")
    with pytest.raises(ManagedRuntimeError):
        storage.bindings(
            replace(
                spec, storage=(VolumeMount(handle, "/workspace"), VolumeMount(handle, "/another"))
            )
        )


@pytest.mark.parametrize("amount", [0, -1, True, 1.5])
def test_storage_reservation_requires_real_positive_integral_disk_capacity(amount):
    with pytest.raises(ManagedRuntimeError):
        VolumeRequest(scope(), str(uuid.uuid4()), amount)
