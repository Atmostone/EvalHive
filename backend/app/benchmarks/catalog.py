"""Read installed data declarations without importing or executing benchmark code."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from pathlib import Path

from evalhive_benchmark import ProtocolError, validate
from jsonschema import Draft202012Validator


class CatalogError(ValueError):
    pass


def check_schema(schema: dict, *, secret: bool = False) -> None:
    """Validate local schemas; public settings and credential schemas stay separate."""
    if schema.get("$schema", "https://json-schema.org/draft/2020-12/schema") != (
        "https://json-schema.org/draft/2020-12/schema"
    ):
        raise CatalogError("Unsupported benchmark schema dialect")
    try:
        Draft202012Validator.check_schema(schema)

        def walk(value):
            if isinstance(value, dict):
                for key in ("$ref", "$dynamicRef"):
                    if key in value and not value[key].startswith("#"):
                        raise CatalogError("Benchmark schemas must use local references")
                if not secret and (
                    value.get("writeOnly") or value.get("x-secret")
                    or value.get("format") == "password"
                ):
                    raise CatalogError("Credentials require a separate secret schema")
                if secret and any(key in value for key in ("default", "examples", "enum", "const")):
                    raise CatalogError("Credential schemas cannot contain sample values")
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(schema)
    except CatalogError:
        raise
    except Exception:
        raise CatalogError("Invalid benchmark configuration schema") from None


def load_catalog(directory: str | Path | None = None, *, kind: str = "benchmark") -> dict[str, dict]:
    if directory is None:
        from app.config import get_settings

        directory = get_settings().benchmark_manifest_dir
    root = Path(directory)
    catalog = {}
    try:
        if not root.is_dir():
            raise CatalogError("Benchmark manifest directory is unavailable")
        for path in sorted(root.glob("*/manifest.json")):
            if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 256 * 1024:
                raise CatalogError("Benchmark manifest must be a bounded installed file")
            manifest = validate("manifest", json.loads(path.read_text(encoding="utf-8")))
            if manifest["kind"] != kind:
                continue
            check_schema(manifest["config_schema"])
            check_schema(manifest.get("secret_schema", {"type": "object"}), secret=True)
            if manifest["id"] in catalog:
                raise CatalogError("Duplicate benchmark identity")
            catalog[manifest["id"]] = manifest
    except CatalogError:
        raise
    except (OSError, ValueError, ProtocolError, RecursionError):
        raise CatalogError("Invalid installed benchmark manifest") from None
    except Exception:
        raise CatalogError("Invalid installed benchmark schema") from None
    return deepcopy(catalog)


def get_manifest(connector_id: str, version: str | None = None, *, directory=None) -> dict:
    manifest = load_catalog(directory).get(connector_id)
    if not manifest:
        raise CatalogError("Benchmark is not installed")
    if version is not None and version != manifest["version"]:
        raise CatalogError("Pinned benchmark version is not installed")
    return manifest


def public_case(descriptor: dict) -> dict:
    """Native locators, hidden metadata and evaluator context are coordinator-only."""
    validate("case_descriptor", descriptor)
    return deepcopy({key: descriptor[key] for key in (
        "ref", "title", "description", "source_digest", "requirements"
    ) if key in descriptor})


def merged_requirements(*values: dict) -> dict:
    """Keep every capability and the greatest native lower bound; never clamp."""
    capabilities: set[str] = set()
    resources: dict = {}
    platforms: list[dict] = []
    for value in values:
        validate("requirements", value)
        capabilities.update(value.get("capabilities", []))
        for key, minimum in value.get("resources", {}).items():
            resources[key] = max(resources.get(key, minimum), minimum)
        if value.get("platform"):
            platforms.append(value["platform"])
    platform = {}
    for item in platforms:
        for key, expected in item.items():
            if key in platform and platform[key] != expected:
                raise CatalogError("Native platform requirements conflict")
            platform[key] = expected
    return {"capabilities": sorted(capabilities), "resources": resources,
            **({"platform": platform} if platform else {})}


def compatibility(requirements: dict, available: dict) -> dict:
    """Pure static admission; the broker must reserve concrete resources at prepare."""
    validate("requirements", requirements)
    reasons = []
    for capability in requirements.get("capabilities", []):
        if capability not in available.get("capabilities", []):
            reasons.append({"code": "missing_capability", "capability": capability})
    for key, expected in requirements.get("platform", {}).items():
        if available.get("platform", {}).get(key) != expected:
            reasons.append({"code": "platform_mismatch", "field": key, "required": expected})
    for key, minimum in requirements.get("resources", {}).items():
        capacity = available.get("resources", {}).get(key)
        measured = (isinstance(capacity, (int, float)) and not isinstance(capacity, bool)
                    and math.isfinite(capacity))
        if not measured or capacity < minimum:
            reasons.append({"code": "insufficient_resources", "resource": key,
                            "required": minimum, "available": capacity if measured else None})
    return {"compatible": not reasons, "requirements": deepcopy(requirements), "reasons": reasons}
