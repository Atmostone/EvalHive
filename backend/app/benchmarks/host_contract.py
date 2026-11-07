"""Capabilities implemented by the product's explicit rootless composition."""

from evalhive_benchmark import validate

from app.runtime.admission import GroupPolicy
from app.runtime.managed_spec import container_capabilities


def group_policy(settings):
    return GroupPolicy(
        max_memory_mb=settings.benchmark_group_memory_mb,
        max_cpu_cores=settings.benchmark_group_cpu_cores,
    )


def capabilities(settings):
    if not settings.benchmark_runtime_enabled:
        return []
    result = [
        "host.agent_api_v1",
        "host.model_api_v1",
        "host.ephemeral_exec_v1",
        "host.exec_deadline_inheritance_v1",
        "host.retained_agent_processes_v1",
        "host.model_gateway_v1",
        "host.rootless_storage_v1",
        "host.rootless_networks_v1",
        "host.container_health_v1",
        "host.volume_copy_up_v1",
    ]
    if settings.benchmark_resource_finalizers:
        result.append("host.resource_finalizers_v1")
    if container_capabilities(settings.benchmark_linux_capabilities):
        result.append("host.linux_capabilities_v1")
    return sorted(result)


def provider_manifest():
    return validate(
        "manifest",
        {
            "protocol_version": "1.0",
            "kind": "environment_provider",
            "id": "evalhive.rootless",
            "version": "1.0.0",
            "display_name": "EvalHive rootless Docker",
            "entrypoint": ["builtin:evalhive.rootless"],
            "image": "builtin:evalhive.rootless",
            "config_schema": {"type": "object", "additionalProperties": False},
            "capabilities": {
                operation: "native"
                for operation in ("allocate", "exec", "inspect", "stop", "export", "release")
            },
            "requirements": {},
        },
    )
