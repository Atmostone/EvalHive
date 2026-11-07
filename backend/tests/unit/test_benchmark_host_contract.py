"""Static host declarations use group policy and never imply a runtime reservation."""

from types import SimpleNamespace

from app.benchmarks.host_contract import capabilities, provider_manifest
from app.benchmarks.preflight import policy_capacity


def settings(tmp_path, **extra):
    return SimpleNamespace(
        benchmark_runtime_enabled=True,
        benchmark_group_memory_mb=4096,
        benchmark_group_cpu_cores=2.0,
        benchmark_resource_finalizers=True,
        benchmark_linux_capabilities=["CHOWN", "SETUID"],
        runtime_max_memory_mb=2048,
        runtime_max_cpu_quota=100000,
        data_dir=str(tmp_path),
        **extra,
    )


def test_group_limits_are_independent_of_one_container_limits(tmp_path, monkeypatch):
    configured = settings(tmp_path)
    monkeypatch.setattr("app.benchmarks.preflight.get_settings", lambda: configured)
    available = policy_capacity()
    assert available["resources"]["memory_mb"] == 4096
    assert available["resources"]["cpu_cores"] == 2
    assert "host.agent_api_v1" in available["capabilities"]
    assert "host.resource_finalizers_v1" in available["capabilities"]


def test_disabled_runtime_or_policy_does_not_advertise_unavailable_features(tmp_path):
    configured = settings(tmp_path)
    configured.benchmark_runtime_enabled = False
    assert capabilities(configured) == []
    configured.benchmark_runtime_enabled = True
    configured.benchmark_resource_finalizers = False
    configured.benchmark_linux_capabilities = []
    available = capabilities(configured)
    assert "host.rootless_storage_v1" in available
    assert "host.resource_finalizers_v1" not in available
    assert "host.linux_capabilities_v1" not in available


def test_builtin_provider_manifest_is_data_and_cannot_be_mutated_through_a_previous_read():
    first = provider_manifest()
    assert first["id"] == "evalhive.rootless" and first["kind"] == "environment_provider"
    assert first["entrypoint"] == ["builtin:evalhive.rootless"]
    first["capabilities"].clear()
    assert provider_manifest()["capabilities"]["release"] == "native"
