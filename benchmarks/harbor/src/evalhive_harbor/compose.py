"""Read a bounded native Compose subset during administrator installation only."""

from __future__ import annotations

import re
import shlex
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import PurePosixPath

from harbor.models.task.verifier_mode import resolve_verifier_environment_definition

from .native import HarborConfigurationError
from .transfers import absolute_path

NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
SERVICE_FIELDS = {
    "image",
    "build",
    "command",
    "entrypoint",
    "working_dir",
    "user",
    "environment",
    "depends_on",
    "volumes",
    "cpus",
    "mem_limit",
}


def native_string(value):
    if not isinstance(value, str) or "\0" in value or "$" in value or len(value) > 4096:
        raise HarborConfigurationError("Native Compose templates require explicit private bindings")
    return value


def argv(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = shlex.split(native_string(value))
        except ValueError:
            raise HarborConfigurationError("Native Compose command quoting is invalid") from None
    if not isinstance(value, list) or len(value) > 64:
        raise HarborConfigurationError("Native Compose command requires a bounded argv")
    return [native_string(item) for item in value]


def limits(settings):
    result = {}
    if "cpus" in settings:
        try:
            value = Decimal(str(settings["cpus"]))
            if not value.is_finite() or value <= 0 or value > 1024:
                raise ValueError
        except (InvalidOperation, ValueError):
            raise HarborConfigurationError("Native Compose CPU limit is invalid") from None
        result["cpu_cores"] = float(value)
    if "mem_limit" in settings:
        value = settings["mem_limit"]
        if type(value) is int and value > 0:
            amount = Decimal(value)
        elif isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?[kmgKMG]", value):
            amount = Decimal(value[:-1]) * (1024 ** ("kmg".index(value[-1].lower()) + 1))
        else:
            raise HarborConfigurationError(
                "Native Compose memory units require an installed adapter"
            )
        if amount <= 0 or amount > 1024**5:
            raise HarborConfigurationError("Native Compose memory limit is invalid")
        result["memory_mb"] = int((amount / 1024**2).to_integral_value(rounding=ROUND_CEILING))
    return result


def service_order(services):
    ordered, visiting = [], set()

    def visit(name):
        if name in visiting:
            raise HarborConfigurationError("Native Compose dependencies contain a cycle")
        if name in ordered:
            return
        visiting.add(name)
        for dependency in services[name].get("depends_on", []):
            visit(dependency)
        visiting.remove(name)
        ordered.append(name)

    for name in sorted(services):
        visit(name)
    return ordered


def read_compose(native, *, verifier=False):
    """No host interpolation, code execution, credential lookup or Docker client."""
    if verifier:
        definition = resolve_verifier_environment_definition(native.task.config, native.task.paths)
        if definition is None:
            return None
        directory = definition.directory
    else:
        directory = native.task.paths.environment_dir
    path = directory / "docker-compose.yaml"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024**2:
        raise HarborConfigurationError("Native Compose source must be a bounded regular file")
    import yaml

    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node)
            if not isinstance(key, str) or key in result:
                raise HarborConfigurationError("Native Compose has duplicate or non-string keys")
            result[key] = loader.construct_object(value_node)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    data = path.read_text()
    try:
        if any(
            isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
            for token in yaml.scan(data)
        ):
            raise HarborConfigurationError("Native Compose inheritance needs its installed adapter")
        document = yaml.load(data, Loader=UniqueLoader)
    except yaml.YAMLError:
        raise HarborConfigurationError("Native Compose syntax is invalid") from None
    if not isinstance(document, dict) or set(document) - {"version", "services", "volumes"}:
        raise HarborConfigurationError("Native Compose has unsupported project settings")
    services, volumes = document.get("services", {}), document.get("volumes", {})
    if (
        not isinstance(services, dict)
        or not 1 <= len(services) <= 16
        or not isinstance(volumes, dict)
        or len(volumes) > 16
    ):
        raise HarborConfigurationError("Native Compose service or volume inventory is invalid")
    for name, settings in volumes.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or settings not in ({}, None):
            raise HarborConfigurationError("Native Compose volumes require scoped local storage")
    normalized = {"main": {}}
    for name, settings in services.items():
        if (
            not isinstance(name, str)
            or not NAME.fullmatch(name)
            or not isinstance(settings, dict)
            or set(settings) - SERVICE_FIELDS
        ):
            raise HarborConfigurationError("Native Compose service has unsupported settings")
        selected = {}
        for key in ("image", "build"):
            if key in settings:
                value = settings[key]
                if not isinstance(value, str):
                    raise HarborConfigurationError(
                        "Native Compose image/build bindings require an installed adapter"
                    )
                if key == "image" and value in {"${MAIN_IMAGE_NAME}", "${PREBUILT_IMAGE_NAME}"}:
                    selected[key] = value
                else:
                    selected[key] = native_string(value)
                if key == "build":
                    context = PurePosixPath(value)
                    if context.is_absolute() or ".." in context.parts:
                        raise HarborConfigurationError(
                            "Native Compose build context leaves its source"
                        )
        for key in ("command", "entrypoint"):
            if key in settings:
                selected[key] = argv(settings[key])
        if "working_dir" in settings:
            selected["workdir"] = absolute_path(native_string(settings["working_dir"]))
        if "user" in settings:
            selected["user"] = native_string(settings["user"])
        environment = settings.get("environment", {})
        if not isinstance(environment, dict):
            raise HarborConfigurationError(
                "Native Compose environment requires explicit string values"
            )
        selected["environment"] = {
            native_string(key): native_string(value) for key, value in environment.items()
        }
        dependencies = settings.get("depends_on", [])
        if (
            not isinstance(dependencies, list)
            or any(
                not isinstance(item, str) or item not in {*services, "main"} or item == name
                for item in dependencies
            )
            or len(set(dependencies)) != len(dependencies)
        ):
            raise HarborConfigurationError(
                "Native Compose dependencies require supported service-start conditions"
            )
        selected["depends_on"] = dependencies
        mounts = []
        native_mounts = settings.get("volumes", [])
        if not isinstance(native_mounts, list) or len(native_mounts) > 16:
            raise HarborConfigurationError("Native Compose mounts exceed supported storage slots")
        for mount in native_mounts:
            if not isinstance(mount, str):
                raise HarborConfigurationError("Native Compose mounts require scoped named volumes")
            parts = mount.split(":")
            if (
                len(parts) not in {2, 3}
                or parts[0] not in volumes
                or len(parts) == 3
                and parts[2] not in {"ro", "rw"}
            ):
                raise HarborConfigurationError(
                    "Native Compose host and anonymous mounts require an installed adapter"
                )
            mounts.append(
                {
                    "volume": parts[0],
                    "target": absolute_path(parts[1]),
                    "read_only": len(parts) == 3 and parts[2] == "ro",
                }
            )
        selected["storage"] = mounts
        if len({mount["target"] for mount in mounts}) != len(mounts):
            raise HarborConfigurationError("Native Compose mounts have conflicting targets")
        selected["native_limits"] = limits(settings)
        normalized[name] = selected
    return {"services": normalized, "volumes": sorted(volumes), "order": service_order(normalized)}
