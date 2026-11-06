"""Explicit versioned JSON for private runtime leases; no executable serialization."""

import json
from dataclasses import asdict

from app.benchmarks.private_store import binary, unbinary
from app.runtime.broker import ResourceLease
from app.runtime.managed_spec import (
    ContainerSpec,
    ExecSpec,
    ManagedContainer,
    ManagedExec,
    ManagedNetwork,
    NetworkRequest,
    OwnerScope,
    Quota,
)
from app.runtime.spec import BindMount


def encode_lease(lease: ResourceLease) -> dict:
    def value(item):
        if item is None:
            return None
        data = asdict(item)
        if isinstance(item, ExecSpec | ManagedExec):
            data["control_key"] = binary(item.control_key)
        if isinstance(item, ExecSpec):
            data["private_inputs"] = {
                name: binary(content) for name, content in item.private_inputs.items()
            }
        return data

    result = {
        "version": 1,
        "handle": lease.handle,
        "scope": asdict(lease.scope),
        "kind": lease.kind,
        "intent": value(lease.intent),
        "native": value(lease.native),
        "state": lease.state,
        "deadline_at": lease.deadline_at,
        "dependencies": sorted(
            {
                value.resource_id
                for value in (
                    (lease.intent.container,)
                    if isinstance(lease.intent, ExecSpec)
                    else (lease.intent.network, lease.intent.namespace)
                    if isinstance(lease.intent, ContainerSpec)
                    else ()
                )
                if value is not None
            }
        ),
    }
    return json.loads(json.dumps(result, allow_nan=False))


def decode_lease(data: dict) -> ResourceLease:
    if data["version"] != 1:
        raise ValueError("Unsupported runtime lease storage version")

    def container(value):
        if value is None:
            return None
        return ManagedContainer(**{**value, "scope": OwnerScope(**value["scope"])})

    def network(value):
        if value is None:
            return None
        return ManagedNetwork(**{**value, "scope": OwnerScope(**value["scope"])})

    kind, raw, native = data["kind"], data["intent"], data["native"]
    scope = OwnerScope(**data["scope"])
    if kind == "network":
        intent = NetworkRequest(**{**raw, "scope": OwnerScope(**raw["scope"])})
        native = network(native)
    elif kind == "container":
        intent = ContainerSpec(
            **{
                **raw,
                "scope": OwnerScope(**raw["scope"]),
                "quota": Quota(**raw["quota"]),
                "mounts": tuple(BindMount(**item) for item in raw["mounts"]),
                "entrypoint": None if raw["entrypoint"] is None else tuple(raw["entrypoint"]),
                "command": None if raw["command"] is None else tuple(raw["command"]),
                "network_aliases": tuple(raw["network_aliases"]),
                "network": network(raw["network"]),
                "namespace": container(raw["namespace"]),
            }
        )
        native = container(native)
    elif kind == "exec":
        intent = ExecSpec(
            **{
                **raw,
                "container": container(raw["container"]),
                "argv": tuple(raw["argv"]),
                "control_key": unbinary(raw["control_key"]),
                "private_inputs": {
                    key: unbinary(value) for key, value in raw["private_inputs"].items()
                },
            }
        )
        if native is not None:
            native = ManagedExec(
                **{
                    **native,
                    "container": container(native["container"]),
                    "argv": tuple(native["argv"]),
                    "control_key": unbinary(native["control_key"]),
                }
            )
    else:
        raise ValueError("Unsupported runtime lease kind")
    return ResourceLease(
        data["handle"], scope, kind, intent, native, data["state"], data["deadline_at"]
    )
