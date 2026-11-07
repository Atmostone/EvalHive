"""Server composition preserves private grants and the native Task boundary."""

import asyncio
import json
import uuid
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.benchmarks.composition import RootlessCoordinator
from app.harnesses.catalog import get_manifest
from app.runtime.broker import ResourceLease
from app.runtime.managed_spec import ContainerSpec, ManagedRuntimeError, PathMapping, Quota

from tests.runtime.authority_fixture import manifest as provider_manifest
from tests.runtime.test_registered_assets import IMAGE
from tests.runtime.test_worker_host_purpose import MANIFEST, invocation


def setup(tmp_path):
    previous, _, plan = invocation(False)
    plan["agent"].update(mode="in_environment", environment_handle="task")
    store, owner, assets = previous.store, previous.owner, previous.assets
    store.lease_seconds, store.engine = 60, object()
    assets.provider_id = "fixture.rootless"
    runtime = assets.runtime
    runtime.policy = replace(runtime.policy, data=PathMapping("/owned", str(tmp_path)))
    evidence = SimpleNamespace(
        store=store, sink=lambda owner: object(), validate_capture=AsyncMock()
    )
    provider = {**provider_manifest(), "id": assets.provider_id}
    coordinator = RootlessCoordinator(runtime, provider, evidence)
    harness = get_manifest("pi")
    bundle = SimpleNamespace(
        connector_id=harness["id"],
        connector_version=harness["version"],
        image_id=IMAGE,
        fingerprint="sha256:" + "b" * 64,
    )
    spec = {
        "protocol_version": "1.0",
        "workspace_id": str(owner.workspace_id),
        "task_id": str(uuid.uuid4()),
        "attempt_id": str(uuid.uuid4()),
        "task": {"prompt": "Replaced by native prepared access"},
        "cwd": "/workspace",
        "config": {},
    }
    options = dict(
        harness_manifest=harness,
        harness_spec=spec,
        bundle=bundle,
        credentials={"model_api_key": "isolated-private-fixture"},
        spool_root=tmp_path / "spool",
        profile_id=uuid.uuid4(),
    )
    return coordinator, store, owner, assets, plan, options


def test_composition_is_lazy_and_credentials_stay_in_host_only_grants(tmp_path):
    coordinator, store, owner, assets, _, options = setup(tmp_path)
    controller = coordinator.execution(store, owner, MANIFEST, assets, **options)
    executor, host = controller.agent_control.executor, controller.host
    assert host.channel is None and host.environment is None and executor.job is None
    assets.compile.assert_not_awaited()
    store.reserve.assert_not_awaited()
    assert len(controller.deadlines) == 1
    assert json.loads(host.host_private_inputs["agent.credentials"]) == options["credentials"]
    assert "isolated-private-fixture" not in json.dumps(executor.spec)
    assert executor.spec["credential_refs"] == {"model_api_key": "runtime-injected"}
    assert executor.profile_id == options["profile_id"]
    assert host.host_auxiliary_disk_mb == executor.spool_disk_mb
    options["harness_spec"]["task"]["prompt"] = "Changed after construction"
    options["credentials"]["model_api_key"] = "Changed after construction"
    assert "Changed after construction" not in json.dumps(executor.spec)
    assert (
        "Changed after construction" not in host.host_private_inputs["agent.credentials"].decode()
    )


def prepared_target(controller, plan):
    host = controller.host
    handle = str(uuid.uuid4())
    scope = host.scope
    native = ContainerSpec(
        scope, handle, IMAGE, quota=Quota(memory_mb=512, cpu_cores=0.25), role="environment"
    )
    target = plan["agent"]["environment_handle"]
    host.plan = plan
    host.environment = SimpleNamespace(
        leases={
            target: (host.assets.provider_id, ResourceLease(handle, scope, "container", native))
        }
    )
    return {
        "ready": True,
        "session_handle": handle,
        "native_state": {},
        "artifacts": [],
        "agent": {
            **deepcopy(plan["agent"]),
            "environment_handle": handle,
            "cwd": "/native/project",
            "prompt": "Native public instruction",
        },
    }


def test_native_target_controls_applied_image_and_actual_resource_provenance(tmp_path):
    coordinator, store, owner, assets, plan, options = setup(tmp_path)
    controller = coordinator.execution(store, owner, MANIFEST, assets, **options)
    prepared = prepared_target(controller, plan)
    controller.host.agent_gateway.configure(prepared)
    applied = controller.agent_control.executor.spec
    assert applied["metadata"]["image_id"] == IMAGE
    assert applied["metadata"]["runtime_bundle_image_id"] == IMAGE
    assert applied["limits"] == {"memory_mb": 512, "cpu_cores": 0.25}
    assert applied["cwd"] == "/native/project"
    assert applied["task"]["prompt"] == "Native public instruction"


@pytest.mark.parametrize("change", ["handle", "role", "provider"])
def test_prepared_target_cannot_select_another_container_or_private_world(tmp_path, change):
    coordinator, store, owner, assets, plan, options = setup(tmp_path)
    controller = coordinator.execution(store, owner, MANIFEST, assets, **options)
    prepared = prepared_target(controller, plan)
    if change == "handle":
        prepared["agent"]["environment_handle"] = str(uuid.uuid4())
    else:
        target = plan["agent"]["environment_handle"]
        provider, lease = controller.host.environment.leases[target]
        controller.host.environment.leases[target] = (
            "another-provider" if change == "provider" else provider,
            replace(lease, intent=replace(lease.intent, role="service"))
            if change == "role"
            else lease,
        )
    with pytest.raises(ManagedRuntimeError):
        controller.host.agent_gateway.configure(prepared)
    assert not controller.agent_control.executor.access_configured


@pytest.mark.parametrize("change", ["bundle", "credential", "spool", "purpose", "runtime"])
def test_invalid_composition_fails_before_admission_or_native_effects(tmp_path, change):
    coordinator, store, owner, assets, _, options = setup(tmp_path)
    if change == "bundle":
        options["bundle"].connector_version = "unavailable"
    elif change == "credential":
        options["credentials"] = {"model_api_key": ["invalid"]}
    elif change == "spool":
        options["spool_root"] = tmp_path / ".." / "outside"
    elif change == "purpose":
        owner = replace(owner, control_id=uuid.uuid4())
    else:
        assets.runtime = object()
    with pytest.raises(ManagedRuntimeError):
        coordinator.execution(store, owner, MANIFEST, assets, **options)
    store.reserve.assert_not_awaited()
    assets.compile.assert_not_awaited()


def test_execution_owner_cannot_be_used_as_an_evaluation_controller(tmp_path):
    coordinator, store, owner, _, _, _ = setup(tmp_path)
    with pytest.raises(ManagedRuntimeError):
        asyncio.run(coordinator.evaluation(store, owner))


@pytest.mark.asyncio
async def test_missing_regrade_evidence_reader_fails_before_loading_or_starting_native_resources(tmp_path, monkeypatch):
    coordinator, store, owner, _, _, _ = setup(tmp_path)
    owner = replace(owner, control_id=uuid.uuid4())
    load = AsyncMock()
    monkeypatch.setattr("app.benchmarks.composition.load_retained_installation", load)
    with pytest.raises(ManagedRuntimeError, match="verified evidence reader"):
        await coordinator.evaluation(store, owner)
    load.assert_not_awaited()
