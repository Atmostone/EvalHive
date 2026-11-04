"""Load administrator-installed declarations without executing connector packages."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from evalhive_harness import ProtocolError, validate
from jsonschema import Draft202012Validator

from app.config import get_settings


class CatalogError(ValueError):
    pass


def _check_schema(schema: dict) -> None:
    if (
        schema.get("$schema", "https://json-schema.org/draft/2020-12/schema")
        != "https://json-schema.org/draft/2020-12/schema"
    ):
        raise CatalogError("Unsupported connector schema dialect")
    Draft202012Validator.check_schema(schema)

    def walk(value):
        if isinstance(value, dict):
            for name in ("$ref", "$dynamicRef"):
                if name in value and not value[name].startswith("#"):
                    raise CatalogError("Connector schemas must use local references")
            if value.get("writeOnly") or value.get("format") == "password" or value.get("x-secret"):
                if any(key in value for key in ("default", "examples", "enum", "const")):
                    raise CatalogError("Secret fields cannot contain example or default values")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)


def load_catalog(directory: str | Path | None = None) -> dict[str, dict]:
    root = Path(directory or get_settings().harness_manifest_dir)
    catalog = {}
    try:
        if not root.is_dir():
            raise CatalogError("Harness manifest directory is unavailable")
        for path in sorted(root.glob("*/manifest.json")):
            if path.stat().st_size > 256 * 1024:
                raise CatalogError("Harness manifest exceeds size limit")
            manifest = validate("manifest", json.loads(path.read_text(encoding="utf-8")))
            _check_schema(manifest["config_schema"])
            for command in manifest.get("commands", {}).values():
                _check_schema(command["input_schema"])
            if manifest["id"] in catalog:
                raise CatalogError("Duplicate harness identity")
            catalog[manifest["id"]] = manifest
    except CatalogError:
        raise
    except (OSError, ValueError, ProtocolError, RecursionError):
        raise CatalogError("Installed harness manifest is invalid") from None
    except Exception:
        # Third-party schema validators may include values in their exceptions.
        raise CatalogError("Installed connector schema is invalid") from None
    return deepcopy(catalog)


def get_manifest(connector_id: str, version: str | None = None) -> dict:
    manifest = load_catalog().get(connector_id)
    if not manifest:
        raise CatalogError("Harness is not installed")
    if version is not None and manifest["version"] != version:
        raise CatalogError("Pinned connector version is not installed")
    return manifest
