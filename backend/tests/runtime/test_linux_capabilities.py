"""Registered container permissions stay bounded by server-owned policy."""

import asyncio
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from app.runtime.broker import ResourceLease
from app.runtime.lease_codec import decode_lease, encode_lease
from app.runtime.managed_docker import RootlessDockerRuntime
from app.runtime.managed_spec import (
    ContainerSpec,
    ManagedRuntimeError,
    OwnerScope,
    PathMapping,
    RuntimePolicy,
)

from tests.runtime.test_registered_assets import catalog, document, plan, scope


@pytest.mark.parametrize(
    "values", [("ALL",), ("SYS_ADMIN",), ("NET_ADMIN",), ("CAP_CHOWN",), ("CHOWN", "CHOWN")]
)
def test_host_and_container_reject_unbounded_or_privileged_permissions(values):
    with pytest.raises(ManagedRuntimeError):
        RuntimePolicy(PathMapping("/owned", "/owned"), allowed_capabilities=values)
    with pytest.raises(ManagedRuntimeError):
        ContainerSpec(scope(), str(uuid.uuid4()), "sha256:" + "a" * 64, capabilities=values)


def test_runtime_requires_an_explicit_allowlist_and_retains_other_isolation(tmp_path):
    calls = []
    image_id = "sha256:" + "a" * 64

    def create(**options):
        calls.append(options)
        return SimpleNamespace(id="native-container", start=lambda: None)

    client = SimpleNamespace(
        info=lambda: {"SecurityOptions": ["name=rootless"]},
        images=SimpleNamespace(
            get=lambda ref: SimpleNamespace(
                id=image_id, attrs={"Os": "linux", "Architecture": "amd64"}
            )
        ),
        containers=SimpleNamespace(create=create),
    )
    policy = RuntimePolicy(PathMapping(str(tmp_path), str(tmp_path)))
    runtime = RootlessDockerRuntime(client, policy)
    spec = ContainerSpec(
        OwnerScope(*(str(uuid.uuid4()) for _ in range(3)), 1),
        str(uuid.uuid4()),
        image_id,
        capabilities=("SETUID", "CHOWN"),
    )
    with pytest.raises(ManagedRuntimeError, match="exceed runtime policy"):
        runtime.create(spec)
    assert calls == []
    runtime.policy = replace(policy, allowed_capabilities=("CHOWN", "SETUID"))
    runtime.create(spec)
    assert calls[0]["cap_add"] == ["CHOWN", "SETUID"]
    assert calls[0]["cap_drop"] == ["ALL"]
    assert calls[0]["security_opt"] == ["no-new-privileges:true"]
    assert calls[0]["network_disabled"] is True and "privileged" not in calls[0]


def test_legacy_leases_remain_empty_and_new_permission_sets_roundtrip():
    owned = scope()
    spec = ContainerSpec(owned, str(uuid.uuid4()), "sha256:" + "a" * 64, capabilities=("CHOWN",))
    encoded = encode_lease(ResourceLease(spec.resource_id, owned, "container", spec))
    assert decode_lease(encoded).intent.capabilities == ("CHOWN",)
    encoded["intent"].pop("capabilities")
    assert decode_lease(encoded).intent.capabilities == ()


def test_only_registered_permissions_can_enter_a_task_plan():
    async def scenario():
        declaration = document()
        declaration["assets"]["fixture.task"]["capabilities"] = ["CHOWN"]
        assets, _ = catalog(declaration)
        with pytest.raises(ManagedRuntimeError, match="exceed runtime policy"):
            await assets.compile(scope(), plan())
        assets.runtime.policy = replace(assets.runtime.policy, allowed_capabilities=("CHOWN",))
        compiled = await assets.compile(scope(), plan())
        assert compiled.leases["task"][1].intent.capabilities == ("CHOWN",)
        forged = plan()
        forged["resources"][0]["config"]["capabilities"] = ["SYS_ADMIN"]
        with pytest.raises(ManagedRuntimeError):
            await assets.compile(scope(), forged)

    asyncio.run(scenario())
