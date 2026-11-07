"""Installed, replay-safe cleanup commands for resources with external effects."""

import json
from dataclasses import asdict

from app.runtime.managed_spec import ContainerSpec, ManagedRuntimeError


def finalizer_payload(lease):
    if not isinstance(lease.intent, ContainerSpec) or lease.intent.finalizer is None:
        raise ManagedRuntimeError("Resource has no installed finalizer")
    selected = lease.intent.finalizer
    return {
        "argv": list(selected.argv),
        "environment": {
            "EVALHIVE_RESOURCE_SCOPE": json.dumps(asdict(lease.scope), sort_keys=True),
            "EVALHIVE_FINALIZER_TIMEOUT": str(selected.timeout_seconds),
        },
        "timeout_seconds": selected.timeout_seconds,
        "deadline_policy": "strict",
        "process_policy": "stop_after_command",
    }
