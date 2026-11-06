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
SCHEMA = json.loads(files("evalhive_harness").joinpath("schemas/contract.json").read_text())
Draft202012Validator.check_schema(SCHEMA)
_registry = Registry().with_resource(SCHEMA["$id"], Resource.from_contents(SCHEMA))
_validators: dict[str, Draft202012Validator] = {}


class ProtocolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self)}


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
    return value


def validate_run(manifest: dict, run: dict) -> dict:
    validate("manifest", manifest)
    validate("run_spec", run)
    from .resources import resources

    resources(run)
    schema = manifest["config_schema"]
    try:
        if (
            schema.get("$schema", "https://json-schema.org/draft/2020-12/schema")
            != "https://json-schema.org/draft/2020-12/schema"
        ):
            raise ValueError("Unsupported schema dialect")
        Draft202012Validator.check_schema(schema)
        valid = Draft202012Validator(schema, registry=Registry()).is_valid(run["config"])
    except Exception:
        raise ProtocolError("INVALID_CONFIG", "Invalid connector configuration schema") from None
    if not valid:
        raise ProtocolError("INVALID_CONFIG", "Configuration does not match connector schema")
    for name in run.get("required_capabilities", []):
        if manifest["capabilities"].get(name) not in ("native", "emulated"):
            raise ProtocolError("UNSUPPORTED_CAPABILITY", f"Unavailable capability: {name}")
    return run


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
    if len(line) > MAX_LINE_BYTES + 1:
        raise ProtocolError("FRAME_TOO_LARGE", "Protocol record exceeds size limit")
    if not line.endswith(b"\n"):
        raise ProtocolError("INVALID_FRAME", "Protocol record must end with LF")
    try:
        value = json.loads(
            line[:-1].removesuffix(b"\r").decode("utf-8"),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("INVALID_FRAME", "Invalid JSON protocol record") from None
    return validate("wire_message", value)
