"""Deployment composition preserves policy and recovers without native packages."""

import uuid
from types import SimpleNamespace

import pytest
from app.benchmarks.product_runtime import ProductRuntime, auxiliary_bindings, private_directory
from app.config import get_settings
from evalhive_benchmark import ProtocolError

from tests.integration.test_benchmark_dispatch import queued as queued
from tests.integration.test_benchmark_launch_inputs import input_selection as input_selection
from tests.unit.test_benchmark_installations import registry as registry


def settings_for(tmp_path, **overrides):
    return get_settings().model_copy(
        update={
            "benchmark_runtime_enabled": True,
            "data_dir": str(tmp_path / "data"),
            "host_data_dir": str(tmp_path / "data"),
            "benchmark_manifest_dir": str(tmp_path / "packages"),
            "host_benchmark_manifest_dir": str(tmp_path / "packages"),
            **overrides,
        }
    )


async def test_recovery_uses_persisted_control_without_loading_a_worker(
    queued, tmp_path, monkeypatch
):
    store, _, trial_id, workspace_id, _ = queued
    owner = await store.claim(workspace_id, trial_id, uuid.uuid4())
    await store.transition(owner, "created", "planning")
    await store.transition(owner, "planning", "cleaning")
    closed = []
    client = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr("app.benchmarks.product_runtime.docker.from_env", lambda: client)
    settings = settings_for(
        tmp_path, benchmark_runtime_enabled=False, host_benchmark_manifest_dir=""
    )
    runtime = ProductRuntime.installed(store, settings=settings, recovery=True)
    controller = runtime.recovery(owner)
    assert not hasattr(controller.host, "manifest")
    assert controller.host.channel is None and controller.agent_control is None
    await controller.run({})
    state = await store.control(owner)
    assert state.phase == "finished" and state.cleanup_status == "complete"
    assert state.execution_status == "failed" and state.recording_status == "complete"
    await runtime.close()
    assert closed == [True]


def test_factory_preserves_host_policy_and_refuses_incomplete_deployment(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.benchmarks.product_runtime.docker.from_env", lambda: pytest.fail("No daemon access")
    )
    for overrides in (
        {"benchmark_runtime_enabled": False},
        {"host_benchmark_manifest_dir": ""},
    ):
        with pytest.raises(ProtocolError) as error:
            ProductRuntime.installed(None, settings=settings_for(tmp_path, **overrides))
        assert error.value.code == "RUNTIME_UNAVAILABLE"
    closed = []
    client = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr("app.benchmarks.product_runtime.docker.from_env", lambda: client)
    store = SimpleNamespace()
    runtime = ProductRuntime.installed(store, settings=settings_for(tmp_path))
    assert runtime.runtime.policy.max_memory_mb == get_settings().runtime_max_memory_mb
    assert runtime.runtime.policy.max_cpu_cores == get_settings().runtime_max_cpu_quota / 100000
    assert set(runtime.runtime.policy.allowed_capabilities) == set(
        get_settings().benchmark_linux_capabilities
    )
    assert runtime.evidence.bucket == get_settings().benchmark_evidence_bucket
    readonly = [item.local for item in runtime.runtime.policy.readonly]
    assert str(tmp_path / "data/benchmark-runtime-bundles") in readonly
    assert str(tmp_path / "data") not in readonly
    assert (
        runtime.coordinator.group_policy.max_memory_mb == get_settings().benchmark_group_memory_mb
    )
    assert runtime.coordinator.limits.stop_seconds == 10
    client.close()


def test_product_shutdown_grace_is_bounded_and_keeps_other_deadlines(tmp_path, monkeypatch):
    from app.benchmarks.driver import DriverLimits

    monkeypatch.setattr(
        "app.benchmarks.product_runtime.docker.from_env",
        lambda: SimpleNamespace(close=lambda: None),
    )
    runtime = ProductRuntime.installed(
        None, settings=settings_for(tmp_path, benchmark_stop_grace_seconds=2.5)
    )
    assert runtime.coordinator.limits.stop_seconds == 2.5
    assert runtime.coordinator.limits.phase_seconds == DriverLimits().phase_seconds
    assert runtime.coordinator.limits.cleanup_seconds == DriverLimits().cleanup_seconds


def test_retained_catalog_roots_allow_old_pinned_inputs_without_neighboring_paths(
    tmp_path, monkeypatch
):
    from app.runtime.file_pins import installed_path
    from app.runtime.managed_spec import ManagedRuntimeError

    monkeypatch.setattr(
        "app.benchmarks.product_runtime.docker.from_env",
        lambda: SimpleNamespace(close=lambda: None),
    )
    old = tmp_path / "previous-release"
    old.mkdir()
    (old / "native.txt").write_text("Frozen native input")
    runtime = ProductRuntime.installed(
        None,
        settings=settings_for(
            tmp_path, benchmark_retained_catalog_roots={"/retained/previous": str(old)}
        ),
    )
    assert (
        installed_path("/retained/previous/native.txt", runtime.runtime.policy)
        == old / "native.txt"
    )
    with pytest.raises(ManagedRuntimeError, match="installed read-only root"):
        installed_path("/retained/neighbor/native.txt", runtime.runtime.policy)


@pytest.mark.parametrize("host", ["/", "relative/path"])
def test_invalid_retained_catalog_root_fails_before_daemon_access(tmp_path, monkeypatch, host):
    from app.runtime.managed_spec import ManagedRuntimeError

    monkeypatch.setattr(
        "app.benchmarks.product_runtime.docker.from_env", lambda: pytest.fail("No daemon access")
    )
    with pytest.raises(ManagedRuntimeError):
        ProductRuntime.installed(
            None,
            settings=settings_for(
                tmp_path, benchmark_retained_catalog_roots={host: str(tmp_path / "old")}
            ),
        )


async def test_storage_readiness_is_checked_without_a_model_or_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.benchmarks.product_runtime.docker.from_env",
        lambda: SimpleNamespace(close=lambda: None),
    )
    runtime = ProductRuntime.installed(SimpleNamespace(), settings=settings_for(tmp_path))
    runtime.evidence.client = SimpleNamespace(bucket_exists=lambda bucket: False)
    with pytest.raises(ProtocolError) as error:
        await runtime.storage_ready()
    assert error.value.code == "STORAGE_UNAVAILABLE"

    def failed(bucket):
        raise RuntimeError("private-storage-credential")

    runtime.evidence.client = SimpleNamespace(bucket_exists=failed)
    with pytest.raises(ProtocolError) as error:
        await runtime.storage_ready()
    assert error.value.code == "STORAGE_UNAVAILABLE" and "private" not in str(error.value)


def test_runtime_directories_reject_symlinks_and_auxiliary_roles_are_explicit(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ProtocolError):
        private_directory(link)
    values = {
        role: {
            "credential_ref": "model." + role,
            "name": "fixture-" + role,
            "endpoint": "http://127.0.0.1:9/v1",
            "api_key": "synthetic-key",
            "phases": ["driving", "evaluating"],
        }
        for role in ("user", "judge")
    }
    selected = auxiliary_bindings(values, roles=["judge"])
    assert set(selected) == {"judge"} and selected["judge"].credential_ref == "model.judge"
    assert auxiliary_bindings(values, roles=[]) == {}
    with pytest.raises(ProtocolError) as error:
        auxiliary_bindings(values, roles=["missing"])
    assert error.value.code == "MODEL_UNAVAILABLE"
