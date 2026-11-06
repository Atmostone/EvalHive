"""Carry native runtime environment settings through private Harness references."""

import hashlib
import json
from copy import deepcopy

from app.benchmarks.trials import failure


def compile_resources(resources):
    declarations, values = deepcopy(resources), {}
    seen = set()
    for resource in declarations:
        if resource["id"] in seen:
            raise failure("AGENT_RESOURCE_CONFLICT", "Native resource identities must be unique")
        seen.add(resource["id"])
        if resource["kind"] != "mcp":
            continue
        config = resource["config"]
        refs = dict(resource.get("credential_refs", {}))
        for section in ("env", "headers"):
            settings = config.pop(section, {})
            if not isinstance(settings, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in settings.items()
            ):
                raise failure(
                    "AGENT_RESOURCE_CONFLICT", "Native runtime settings must be string mappings"
                )
            for key, value in settings.items():
                target = section + "." + key
                if target in refs:
                    raise failure(
                        "AGENT_RESOURCE_CONFLICT", "Native runtime input has conflicting references"
                    )
                reference = (
                    "benchmark_resource_"
                    + hashlib.sha256(
                        json.dumps([resource["id"], section, key], separators=(",", ":")).encode()
                    ).hexdigest()
                )
                refs[target] = reference
                values[reference] = value
        if refs:
            resource["credential_refs"] = refs
    return declarations, values


def private_inputs(inputs, reference, values):
    """This is transport isolation; callers still identify actual secrets for redaction.

    Native env/header settings are runtime inputs. Owner credentials must already
    use private references and the executor's explicit private_values redaction.
    Never turn arbitrary native settings such as '/workspace' or '1' into global
    redaction patterns.
    """
    if not values:
        return dict(inputs)
    try:
        data = inputs[reference]
        if type(data) is not bytes or len(data) > 1024**2:
            raise ValueError
        credentials = json.loads(data)
        if (
            not isinstance(credentials, dict)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in credentials.items()
            )
            or any(not isinstance(value, str) for value in values.values())
        ):
            raise ValueError
        if set(credentials) & set(values):
            raise ValueError
        encoded = json.dumps({**credentials, **values}, separators=(",", ":")).encode()
        if len(encoded) > 1024**2:
            raise ValueError
        return {**inputs, reference: encoded}
    except (KeyError, TypeError, ValueError, RecursionError):
        raise failure(
            "AGENT_CREDENTIALS_INVALID",
            "Native runtime inputs require a bounded host credential grant",
        ) from None
