"""Translate native task resources into the public Harness SDK boundary."""

import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from .native import HarborConfigurationError
from .transfers import TransferError, absolute_path


def agent_resources(native, cwd):
    resources, tools = [], ["bash"]
    names = {"tools", "native-skills"}
    for server in native.task.config.environment.mcp_servers:
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", server.name)
            or server.name in names
        ):
            raise HarborConfigurationError("Native MCP namespaces require unique portable names")
        names.add(server.name)
        if server.transport == "stdio":
            config = {
                "transport": "stdio",
                "command": server.command,
                "args": list(server.args),
                "cwd": cwd,
            }
        elif server.transport == "streamable-http":
            try:
                address = urlsplit(server.url)
                if (
                    address.scheme not in {"http", "https"}
                    or not address.hostname
                    or address.username is not None
                    or address.password is not None
                    or address.query
                    or address.fragment
                ):
                    raise ValueError
                _ = address.port
            except ValueError:
                raise HarborConfigurationError(
                    "Native MCP URL requires a portable public endpoint"
                ) from None
            if any("${" in value for value in server.headers.values()):
                raise HarborConfigurationError("Native MCP headers require private bindings")
            config = {"transport": "http", "url": server.url, "headers": dict(server.headers)}
        else:
            raise HarborConfigurationError("Native SSE requires an installed transport adapter")
        resources.append({"id": server.name, "kind": "mcp", "config": config})
        tools.append("mcp__" + server.name + "__*")
    skills = native.task.config.environment.skills_dir
    if skills:
        path = PurePosixPath(skills)
        resolved = str(path if path.is_absolute() else PurePosixPath(cwd) / path)
        try:
            path = absolute_path(resolved)
        except TransferError:
            raise HarborConfigurationError("Native skills need a portable task path") from None
        resources.append({"id": "native-skills", "kind": "skill", "config": {"path": path}})
    return [{"id": "tools", "kind": "tools", "config": {"names": tools}}, *resources]
