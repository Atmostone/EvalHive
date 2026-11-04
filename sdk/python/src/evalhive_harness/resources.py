"""Harness-neutral resource validation and private credential resolution."""

from copy import deepcopy

from .protocol import ProtocolError, validate


def resources(spec: dict) -> list[dict]:
    values = spec.get("environment", {}).get("resources", [])
    seen, singleton = set(), set()
    for resource in values:
        validate("resource", resource)
        if resource["id"] in seen or (
            resource["kind"] in {"tools", "memory"} and resource["kind"] in singleton
        ):
            raise ProtocolError(
                "DUPLICATE_RESOURCE", "Resource identities and singleton kinds must be unique"
            )
        seen.add(resource["id"])
        singleton.add(resource["kind"])
    return values


def resource_config(resource: dict, credentials: dict[str, str]) -> dict:
    validate("resource", resource)
    config = deepcopy(resource["config"])
    for target, reference in resource.get("credential_refs", {}).items():
        if not isinstance(credentials.get(reference), str):
            raise ProtocolError("CREDENTIAL_REQUIRED", "Resource credential is unavailable")
        section, name = target.split(".", 1)
        if (section == "env") != (config["transport"] == "stdio"):
            raise ProtocolError(
                "INVALID_RESOURCE", "Credential target does not match the MCP transport"
            )
        config.setdefault(section, {})[name] = credentials[reference]
    return config
