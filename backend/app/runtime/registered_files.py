"""Load installed relative file grants and host-supplied runtime bindings as data."""

from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator

from app.runtime.file_pins import installed_path
from app.runtime.managed_spec import ManagedRuntimeError, PathMapping
from app.runtime.registered_assets import ID, RegisteredFile, RootlessAssets, obj

FILE_MAP_SCHEMA = obj(
    {
        "format_version": {"const": "1.0"},
        "files": {
            "type": "object",
            "maxProperties": 256,
            "propertyNames": ID,
            "additionalProperties": {
                "oneOf": [
                    obj(
                        {
                            "kind": {"const": "relative"},
                            "path": {"type": "string", "minLength": 1, "maxLength": 4096},
                            "source_digest": {"type": "string", "pattern": "^sha256:[a-f0-9]{64}$"},
                        },
                        ("kind", "path", "source_digest"),
                    ),
                    obj(
                        {"kind": {"const": "runtime_binding"}, "name": ID},
                        ("kind", "name"),
                    ),
                ]
            },
        },
    },
    ("format_version", "files"),
)


def load_registered_files(document, package: PathMapping, policy, *, bindings=None):
    """Bind only installed paths; benchmark workers cannot create these grants.

    Source digests identify the native installation. Byte inventories are frozen
    separately by the worker host before use and retained for exact regrading.
    """
    if not Draft202012Validator(FILE_MAP_SCHEMA).is_valid(document):
        raise ManagedRuntimeError("Invalid installed file map")
    result, bindings = {}, dict(bindings or {})
    root = Path(package.local)
    try:
        for file_id, record in document["files"].items():
            if record["kind"] == "runtime_binding":
                grant = bindings.get(record["name"])
                if not isinstance(grant, RegisteredFile) or grant.writable:
                    raise ManagedRuntimeError("Installed runtime file binding is unavailable")
            else:
                relative = PurePosixPath(record["path"])
                if (
                    relative.is_absolute()
                    or not relative.parts
                    or ".." in relative.parts
                    or str(relative) != record["path"]
                    or "\\" in record["path"]
                ):
                    raise ManagedRuntimeError(
                        "Installed file map requires contained relative paths"
                    )
                local = root / str(relative)
                grant = RegisteredFile(
                    str(PurePosixPath(package.host) / relative), record["source_digest"]
                )
                if installed_path(grant.host_path, policy) != local:
                    raise ManagedRuntimeError("Installed file mapping differs from runtime policy")
            local = installed_path(grant.host_path, policy)
            if not local.is_file() and not local.is_dir():
                raise ManagedRuntimeError("Installed file is unavailable")
            result[file_id] = grant
    except (OSError, ValueError):
        raise ManagedRuntimeError("Installed file map is unavailable") from None
    return result


def load_registered_assets(package: PathMapping, runtime, manifest, *, bindings=None):
    """Load one installed runtime package without importing connector modules."""
    import json

    path = Path(package.local) / "files.json"
    try:
        if path.resolve(strict=True) != path or not path.is_file() or path.stat().st_size > 1024**2:
            raise ValueError
        document = json.loads(path.read_bytes())
        files = load_registered_files(document, package, runtime.policy, bindings=bindings)
        return RootlessAssets.load(
            Path(package.local) / "resources.json",
            runtime,
            files=files,
            connector_id=manifest["id"],
            connector_version=manifest["version"],
        )
    except (OSError, ValueError, RecursionError):
        raise ManagedRuntimeError("Installed runtime package is unavailable") from None
