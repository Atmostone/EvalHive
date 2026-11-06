"""The trusted host binds worker plans to the controller's execution purpose."""

import asyncio
import uuid
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.benchmarks.trials import Controller
from app.benchmarks.worker_host import RootlessWorkerHost
from app.runtime.admission import ResourceDemand
from evalhive_benchmark import ProtocolError

from tests.fixtures.benchmark_driver_worker import AGENT, MANIFEST
from tests.runtime.test_registered_assets import POLICY, QUOTA


def invocation(evaluation):
    owner = Controller(
        uuid.uuid4(), uuid.uuid4(), 1, uuid.uuid4(), uuid.uuid4() if evaluation else None
    )
    spec = {
        "protocol_version": "1.0",
        "scope": {
            "workspace_id": str(owner.workspace_id),
            "trial_id": str(owner.trial_id),
            "generation": 1,
        },
        "case": {
            "ref": {"dataset_id": "fixture", "revision": "1", "case_id": "one"},
            "title": "Fixture",
            "source_digest": "sha256:" + "a" * 64,
            "native_ref": {},
            "requirements": {},
        },
        "config": {},
        "models": {},
        "credential_refs": {},
        "limits": {"wall_time_seconds": 60, "memory_mb": 1024, "cpu_cores": 1, "disk_mb": 64},
    }
    plan = {"resources": [], "outputs": [], "requirements": {}, "grading_mode": "snapshot"}
    if evaluation:
        spec.update(
            purpose="evaluation",
            checkpoint={
                "format_version": "1.0",
                "connector": {key: MANIFEST[key] for key in ("id", "version")},
                "case": deepcopy(spec["case"]["ref"]),
                "source_digest": spec["case"]["source_digest"],
                "state_schema": "fixture.saved.v1",
                "complete": True,
                "artifacts": [],
                "evaluator_roles": [],
                "state": {},
            },
        )
        plan["purpose"] = "evaluation"
    else:
        plan["agent"] = deepcopy(AGENT)
    compiled = SimpleNamespace(
        demands=(),
        leases={},
        images={},
        files={},
        links={},
        fingerprint="fixture",
        output_grants=lambda actors: {},
    )
    assets = SimpleNamespace(
        document={"connector_id": MANIFEST["id"], "connector_version": MANIFEST["version"]},
        provider_id="fixture.rootless",
        compile=AsyncMock(return_value=compiled),
        runtime=SimpleNamespace(policy=POLICY),
    )
    store = SimpleNamespace(reserve=AsyncMock())
    host = RootlessWorkerHost(store, owner, MANIFEST, assets, {})
    host.channel = object()
    host.worker_demand = ResourceDemand("sdk.worker", QUOTA, 256, 0.25, 16)
    return host, spec, plan


@pytest.mark.parametrize("evaluation", [False, True])
def test_controller_rejects_the_other_purpose_before_compilation_or_admission(evaluation):
    async def scenario():
        host, _, _ = invocation(evaluation)
        _, spec, plan = invocation(not evaluation)
        spec["scope"] = {
            "workspace_id": str(host.owner.workspace_id),
            "trial_id": str(host.owner.trial_id),
            "generation": 1,
        }
        with pytest.raises(ProtocolError) as caught:
            await host.install_plan(spec, plan)
        assert caught.value.code == "PURPOSE_CONFLICT"
        host.assets.compile.assert_not_awaited()
        host.store.reserve.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("evaluation", [False, True])
def test_admitted_session_keeps_purpose_and_evaluator_cannot_drive(evaluation, monkeypatch):
    async def scenario():
        install = AsyncMock()
        monkeypatch.setattr("app.benchmarks.worker_host.install_session", install)
        host, spec, plan = invocation(evaluation)
        await host.install_plan(spec, plan)
        host.assets.compile.assert_awaited_once()
        host.store.reserve.assert_awaited_once()
        assert install.await_args.kwargs["purpose"] == ("evaluation" if evaluation else "execution")
        for policy in install.await_args.kwargs["actors"].values():
            assert ("driving" in policy.phases) is (not evaluation)

    asyncio.run(scenario())


def test_retained_archive_and_spool_must_fit_the_trial_before_admission_or_download():
    async def scenario():
        host, spec, plan = invocation(True)
        spec["checkpoint"]["artifacts"] = [
            {
                "id": "checkpoint",
                "uri": "artifact:" + str(uuid.uuid4()),
                "digest": "sha256:" + "a" * 64,
                "size_bytes": 34 * 1024**2,
                "visibility": "private",
                "media_type": "application/octet-stream",
            }
        ]
        plan["resources"] = [
            {
                "id": "grader",
                "provider": "fixture.rootless",
                "kind": "container",
                "config": {"asset_id": "fixture.grader", "retained_inputs": ["checkpoint"]},
            }
        ]
        host.retained_inputs.materialize = AsyncMock()
        with pytest.raises(ProtocolError) as caught:
            await host.install_plan(spec, plan)
        assert caught.value.code == "LIMIT_EXCEEDED"
        host.store.reserve.assert_not_awaited()
        host.retained_inputs.materialize.assert_not_awaited()

    asyncio.run(scenario())


def test_host_credentials_are_not_granted_to_the_benchmark_worker(monkeypatch):
    async def scenario():
        install = AsyncMock()
        monkeypatch.setattr("app.benchmarks.worker_host.install_session", install)
        host, spec, plan = invocation(False)
        original = deepcopy(spec)
        host.host_private_inputs = {"agent.credentials": b"host-private-fixture"}
        await host.install_plan(
            spec, plan, private_inputs={"simulator.credentials": b"worker-private-fixture"}
        )
        actors = install.await_args.kwargs["actors"]
        assert actors[host.worker_actor].private_input_refs == ("simulator.credentials",)
        assert actors[host.host_actor].private_input_refs == (
            "simulator.credentials",
            "agent.credentials",
        )
        assert install.await_args.kwargs["private_inputs"] == {
            "simulator.credentials": b"worker-private-fixture",
            "agent.credentials": b"host-private-fixture",
        }
        assert spec == original and "agent.credentials" not in str(spec)

    asyncio.run(scenario())


def test_private_input_collisions_are_rejected_before_compilation_and_admission():
    async def scenario():
        from app.runtime.managed_spec import ManagedRuntimeError

        host, spec, plan = invocation(False)
        host.host_private_inputs = {"same": b"host-private-fixture"}
        with pytest.raises(ManagedRuntimeError, match="distinct"):
            await host.install_plan(spec, plan, private_inputs={"same": b"worker-private-fixture"})
        host.assets.compile.assert_not_awaited()
        host.store.reserve.assert_not_awaited()

    asyncio.run(scenario())


def test_evaluation_controller_rejects_host_agent_credentials():
    from app.runtime.managed_spec import ManagedRuntimeError

    host, _, _ = invocation(True)
    with pytest.raises(ManagedRuntimeError, match="execution controller"):
        RootlessWorkerHost(
            host.store,
            host.owner,
            host.manifest,
            host.assets,
            {},
            host_private_inputs={"agent.credentials": b"host-private-fixture"},
        )


def test_agent_factory_cannot_bind_another_worker_identity():
    from app.runtime.managed_spec import ManagedRuntimeError

    host, _, _ = invocation(False)

    def foreign_gateway(selected):
        return SimpleNamespace(
            calls=SimpleNamespace(
                trials=selected.store, owner=selected.owner, actor_id=uuid.uuid4()
            )
        )

    with pytest.raises(ManagedRuntimeError, match="ownership"):
        RootlessWorkerHost(
            host.store, host.owner, host.manifest, host.assets, {}, agent_factory=foreign_gateway
        )


def test_evaluation_controller_never_invokes_an_agent_factory():
    from app.runtime.managed_spec import ManagedRuntimeError

    host, _, _ = invocation(True)
    called = []

    def factory(selected):
        called.append(selected)
        raise AssertionError("An evaluation controller must not construct an agent")

    with pytest.raises(ManagedRuntimeError, match="execution controller"):
        RootlessWorkerHost(
            host.store, host.owner, host.manifest, host.assets, {}, agent_factory=factory
        )
    assert not called


def test_host_journal_disk_is_reserved_with_the_full_group(monkeypatch):
    async def scenario():
        monkeypatch.setattr("app.benchmarks.worker_host.install_session", AsyncMock())
        host, spec, plan = invocation(False)
        host.host_auxiliary_disk_mb = 24
        await host.install_plan(spec, plan)
        assert host.store.reserve.await_args.kwargs["auxiliary_disk_mb"] == 24

    asyncio.run(scenario())


def test_host_journal_disk_cannot_bypass_the_trial_budget():
    async def scenario():
        host, spec, plan = invocation(False)
        host.host_auxiliary_disk_mb = 64
        with pytest.raises(ProtocolError) as budget:
            await host.install_plan(spec, plan)
        assert budget.value.code == "LIMIT_EXCEEDED"
        host.store.reserve.assert_not_awaited()

    asyncio.run(scenario())
