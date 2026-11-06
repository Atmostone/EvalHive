"""Translate the pinned native stdio server declarations inside the connector."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from evalhive_cases.format import UniqueLoader

from .commands import NativeConfigurationError

VARIABLES = {
    "local_servers_paths": "/opt/local_servers",
    "agent_workspace": "/workspace",
    "task_dir": "/workspace",
}


def substitute(value):
    if not isinstance(value, str) or "\0" in value:
        raise NativeConfigurationError("Native MCP arguments must be strings")
    for name, replacement in VARIABLES.items():
        value = value.replace("${" + name + "}", replacement)
    if "${" in value:
        raise NativeConfigurationError("Native MCP configuration has an unresolved variable")
    return value


def resource(document):
    if not isinstance(document, dict) or document.get("type", "stdio") != "stdio":
        raise NativeConfigurationError("Native MCP requires an installed stdio declaration")
    if set(document) - {
        "type",
        "name",
        "params",
        "client_session_timeout_seconds",
        "cache_tools_list",
    }:
        raise NativeConfigurationError("Native MCP declaration has unsupported fields")
    if document.get("cache_tools_list", True) is not True:
        raise NativeConfigurationError("Native dynamic MCP tool discovery is unsupported")
    name = document.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", name):
        raise NativeConfigurationError("Native MCP server name is invalid")
    params = document.get("params")
    if not isinstance(params, dict) or set(params) - {"command", "args", "env", "cwd"}:
        raise NativeConfigurationError("Native MCP process fields are unsupported")
    command = substitute(params.get("command"))
    arguments = params.get("args", [])
    if (
        not command
        or any(character.isspace() for character in command)
        or not isinstance(arguments, list)
    ):
        raise NativeConfigurationError("Native MCP executable is invalid")
    arguments = [substitute(argument) for argument in arguments]
    native_env = params.get("env", {})
    if (
        not isinstance(native_env, dict)
        or any(not isinstance(key, str) for key in native_env)
        or any(not isinstance(value, str) for value in native_env.values())
    ):
        raise NativeConfigurationError("Native MCP environment must contain string values")
    environment = {key: substitute(value) for key, value in native_env.items()}
    if any(
        key.startswith("PG_") or key in {"PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"}
        for key in environment
    ):
        # Dedicated, isolated benchmark fixture database; never an owner's account.
        values = {
            "HOST": "toolathlon_pg",
            "PORT": "5432",
            "DATABASE": "toolathlon_gym",
            "USER": "eigent",
            "PASSWORD": "camel",
        }
        environment.update({"PG_" + key: value for key, value in values.items()})
        environment.update({"PG" + key: value for key, value in values.items()})
    if command == "uv":
        environment.update(UV_OFFLINE="1", UV_NO_SYNC="1")
    timeout = document.get("client_session_timeout_seconds", 100)
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise NativeConfigurationError("Native MCP timeout is outside the Harness contract")
    config = {
        "transport": "stdio",
        "command": command,
        "args": arguments,
        "env": environment,
        "timeout_seconds": timeout,
        "alias_prefix": name + "_",
    }
    if "cwd" in params:
        config["cwd"] = substitute(params["cwd"])
    return {"id": "toolathlon-" + name, "kind": "mcp", "config": config}


def load_resources(source, needed):
    root = Path(source).resolve(strict=True)
    directory = root / "configs/mcp_servers"
    if directory.is_symlink() or not directory.is_dir() or directory.resolve() != directory:
        raise NativeConfigurationError("Native MCP declarations are unavailable")
    catalog = {}
    for path in sorted(directory.glob("*.yaml")):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024**2:
            raise NativeConfigurationError("Native MCP declaration must be a bounded regular file")
        try:
            with path.open("rb") as stream:
                data = stream.read(1024**2 + 1)
            if len(data) > 1024**2:
                raise NativeConfigurationError("Native MCP declaration exceeds its byte limit")
            document = yaml.load(data, Loader=UniqueLoader)
            mapped = resource(document)
        except NativeConfigurationError as error:
            raise NativeConfigurationError(path.name + ": " + str(error)) from None
        except (ValueError, TypeError, yaml.YAMLError, RecursionError):
            raise NativeConfigurationError(
                "Native MCP declaration is invalid: " + path.name
            ) from None
        name = document["name"]
        if name in catalog:
            raise NativeConfigurationError("Installed native MCP names must be unique")
        catalog[name] = mapped
    if (
        not isinstance(needed, list)
        or any(not isinstance(name, str) for name in needed)
        or len(needed) != len(set(needed))
        or any(name not in catalog for name in needed)
    ):
        raise NativeConfigurationError("Required native MCP server is not installed uniquely")
    return [catalog[name] for name in needed]
