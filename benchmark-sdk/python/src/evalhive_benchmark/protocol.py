"""Canonical JSON Schema validation and strict LF-delimited JSON transport."""

from __future__ import annotations

import json
import math
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

PROTOCOL_VERSION = "1.0"
MAX_LINE_BYTES = 1024 * 1024
SCHEMA = json.loads(files("evalhive_benchmark").joinpath("schemas/contract.json").read_text())
Draft202012Validator.check_schema(SCHEMA)
_registry = Registry().with_resource(SCHEMA["$id"], Resource.from_contents(SCHEMA))
_validators: dict[str, Draft202012Validator] = {}


class ProtocolError(Exception):
    def __init__(self, code: str, message: str, kind: str = "transport"):
        super().__init__(message)
        self.code = code
        if kind not in {
            "configuration",
            "unsupported",
            "preparation",
            "execution",
            "grading",
            "recording",
            "cleanup",
            "transport",
            "uncertain",
        }:
            raise ValueError("Unknown protocol error kind")
        self.kind = kind

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "kind": self.kind}


def _json_value(value: Any, seen: set[int] | None = None, depth: int = 0) -> None:
    """Reject Python-only values, NaN and unsafe cross-language integers."""
    if depth > 64:
        raise ProtocolError("INVALID_MESSAGE", "JSON nesting exceeds limit")
    if isinstance(value, str):
        if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ProtocolError("INVALID_MESSAGE", "String is not valid Unicode")
        return
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if abs(value) > 2**53 - 1 or not math.isfinite(value):
            raise ProtocolError("INVALID_MESSAGE", "Number is not finite or safely representable")
        return
    if isinstance(value, list | dict):
        seen = set() if seen is None else seen
        if id(value) in seen:
            raise ProtocolError("INVALID_MESSAGE", "Value is not JSON-compatible")
        seen.add(id(value))
        if isinstance(value, dict):
            for key in value:
                if not isinstance(key, str):
                    raise ProtocolError("INVALID_MESSAGE", "Object key must be a string")
                _json_value(key, seen, depth + 1)
            items = value.values()
        else:
            items = value
        for item in items:
            _json_value(item, seen, depth + 1)
        seen.remove(id(value))
        return
    raise ProtocolError("INVALID_MESSAGE", "Value is not JSON-compatible")


def validate(kind: str, value: Any) -> Any:
    _json_value(value)
    if isinstance(value, dict) and "protocol_version" in value:
        if value["protocol_version"] != PROTOCOL_VERSION:
            raise ProtocolError("UNSUPPORTED_PROTOCOL", "Unsupported protocol version")
    if kind not in SCHEMA["$defs"]:
        raise ValueError(f"Unknown contract type: {kind}")
    if kind not in _validators:
        _validators[kind] = Draft202012Validator(
            {"$ref": f"{SCHEMA['$id']}#/$defs/{kind}"}, registry=_registry
        )
    if not _validators[kind].is_valid(value):
        # Validation exceptions may contain credentials from the failing value.
        raise ProtocolError("INVALID_MESSAGE", f"Invalid {kind} shape")
    if kind == "environment_plan":
        _validate_plan(value)
    if kind in {"evaluation_result", "aggregate_result"}:
        metrics = [item["name"] for item in value["metrics"]]
        if len(set(metrics)) != len(metrics):
            raise ProtocolError("DUPLICATE_METRIC", "Metric identities must be unique")
        if value.get("primary_metric") is not None and value["primary_metric"] not in metrics:
            raise ProtocolError("INVALID_METRIC", "Primary metric is not reported")
    return value


def _validate_plan(plan: dict) -> None:
    resources = {item["id"]: item for item in plan["resources"]}
    if len(resources) != len(plan["resources"]):
        raise ProtocolError("DUPLICATE_RESOURCE", "Resource identities must be unique")
    visiting, visited = set(), set()

    def walk(identity):
        if identity in visiting:
            raise ProtocolError("RESOURCE_CYCLE", "Resource dependencies contain a cycle")
        if identity in visited:
            return
        if identity not in resources:
            raise ProtocolError("MISSING_RESOURCE", "Resource dependency is not declared")
        visiting.add(identity)
        for dependency in resources[identity].get("depends_on", []):
            walk(dependency)
        visiting.remove(identity)
        visited.add(identity)

    for identity in resources:
        walk(identity)
    outputs = [item["id"] for item in plan["outputs"]]
    if len(outputs) != len(set(outputs)):
        raise ProtocolError("DUPLICATE_RESOURCE", "Output identities must be unique")
    if any(item["resource_id"] not in resources for item in plan["outputs"]):
        raise ProtocolError("MISSING_RESOURCE", "Output resource is not declared")


def validate_config(schema: dict, config: dict) -> dict:
    """Validate public settings without resolving external schema references."""
    _json_value(schema)
    _json_value(config)
    try:
        if (
            schema.get("$schema", "https://json-schema.org/draft/2020-12/schema")
            != "https://json-schema.org/draft/2020-12/schema"
        ):
            raise ValueError("Unsupported schema dialect")

        def check(value):
            if isinstance(value, dict):
                for key in ("$ref", "$dynamicRef"):
                    if key in value and not value[key].startswith("#"):
                        raise ValueError("Only local schema references are allowed")
                for child in value.values():
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)

        check(schema)
        Draft202012Validator.check_schema(schema)
        valid = Draft202012Validator(schema, registry=Registry()).is_valid(config)
    except Exception:
        raise ProtocolError("INVALID_CONFIG", "Invalid connector configuration schema") from None
    if not valid:
        raise ProtocolError("INVALID_CONFIG", "Configuration does not match connector schema")
    return config


def validate_spec(manifest: dict, spec: dict) -> dict:
    validate("manifest", manifest)
    validate("trial_spec", spec)
    validate_config(manifest["config_schema"], spec["config"])
    from urllib.parse import urlsplit

    for model in spec["models"].values():
        try:
            endpoint = urlsplit(model["endpoint"])
            valid = (
                endpoint.scheme in {"https", "http"}
                and endpoint.netloc
                and not any(
                    (endpoint.username, endpoint.password, endpoint.query, endpoint.fragment)
                )
            )
        except ValueError:
            valid = False
        if not valid:
            raise ProtocolError(
                "INVALID_CONFIG", "Model endpoint contains invalid or private URL components"
            )
        if model["credential_ref"] not in spec["credential_refs"]:
            raise ProtocolError("CREDENTIAL_REQUIRED", "Model credential reference is unavailable")
    return spec


def command(request_id: str, method: str, params: dict | None = None) -> dict:
    return validate(
        "command",
        {
            "protocol_version": PROTOCOL_VERSION,
            "type": "command",
            "id": request_id,
            "method": method,
            "params": params or {},
        },
    )


def response(request_id: str, result: Any) -> dict:
    return validate(
        "response",
        {
            "protocol_version": PROTOCOL_VERSION,
            "type": "response",
            "id": request_id,
            "ok": True,
            "result": result,
        },
    )


def error_response(request_id: str | None, error: ProtocolError) -> dict:
    return validate(
        "response",
        {
            "protocol_version": PROTOCOL_VERSION,
            "type": "response",
            "id": request_id,
            "ok": False,
            "error": error.as_dict(),
        },
    )


def encode_line(value: dict) -> bytes:
    validate("wire_message", value)
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > MAX_LINE_BYTES:
        raise ProtocolError("FRAME_TOO_LARGE", "Protocol record exceeds size limit")
    return data + b"\n"


def decode_line(line: bytes) -> dict:
    if len(line) > MAX_LINE_BYTES + 2:
        raise ProtocolError("FRAME_TOO_LARGE", "Protocol record exceeds size limit")
    if not line.endswith(b"\n"):
        raise ProtocolError("INVALID_FRAME", "Protocol record must end with LF")
    payload = line[:-1].removesuffix(b"\r")
    if len(payload) > MAX_LINE_BYTES:
        raise ProtocolError("FRAME_TOO_LARGE", "Protocol record exceeds size limit")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("INVALID_FRAME", "Invalid JSON protocol record") from None
    return validate("wire_message", value)
