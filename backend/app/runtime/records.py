"""Provider-independent private resource record metadata.

Only the provider interprets intent/native payloads. The coordinator needs the
scoped identity and declared dependency handles to order recovery operations.
"""

import uuid

from app.runtime.managed_spec import ManagedRuntimeError, OwnerScope


def resource_dependencies(record, handle, scope):
    try:
        if (
            record["version"] != 1
            or record["handle"] != handle
            or OwnerScope(**record["scope"]) != scope
        ):
            raise ValueError
        dependencies = record["dependencies"]
        if (
            not isinstance(dependencies, list)
            or len(dependencies) > 256
            or any(
                not isinstance(value, str) or str(uuid.UUID(value)) != value
                for value in dependencies
            )
            or len(set(dependencies)) != len(dependencies)
        ):
            raise ValueError
        return set(dependencies)
    except (KeyError, TypeError, ValueError):
        raise ManagedRuntimeError(
            "Resource recovery metadata is invalid or requires migration"
        ) from None
