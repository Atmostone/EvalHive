"""Launch the installed native MCP server using a read-only prepared connection."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

CONNECTION = Path("/evalhive-connection/connection.json")
CLIENTS = Path("/opt/mcp-clients/node_modules/.bin")


def launch_config(connection):
    if not isinstance(connection, dict) or set(connection) != {"service", "config"}:
        raise ValueError("Prepared native connection is invalid")
    service, config = connection["service"], connection["config"]
    if not isinstance(config, dict):
        raise ValueError("Prepared native service settings are invalid")
    if service == "filesystem":
        if set(config) != {"test_directory"}:
            raise ValueError("Prepared filesystem settings are invalid")
        target = Path(config["test_directory"])
        root = Path("/opt/native/.mcpmark_backups")
        if not target.is_relative_to(root) or target == root or ".." in target.parts:
            raise ValueError("Prepared filesystem directory is unavailable")
        return [str(CLIENTS / "mcp-server-filesystem"), str(target)], {}
    raise ValueError("Native MCP client runtime is not installed for this service")


def main():
    try:
        descriptor = os.open(CONNECTION, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
                raise ValueError("Prepared native connection exceeds its budget")
            data = stream.read(64 * 1024 + 1)
            if len(data) != info.st_size:
                raise ValueError("Prepared native connection changed")
        argv, private_env = launch_config(json.loads(data))
        os.execve(argv[0], argv, {**os.environ, **private_env})
    except Exception:
        sys.stderr.write("Prepared native MCP client could not start\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
