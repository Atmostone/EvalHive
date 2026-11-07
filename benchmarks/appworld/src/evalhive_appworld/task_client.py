"""Fixed standalone entrypoint for official MCP without task data or controls."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

CONNECTION = Path("/evalhive-connection/connection.json")


def read_configuration(path=CONNECTION):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 65536:
            raise ValueError("Native MCP configuration exceeds its regular file budget")
        raw = stream.read(65537)
    if len(raw) != before.st_size:
        raise ValueError("Native MCP configuration changed during reading")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"format_version", "service", "source_digest", "api_url", "groups"}
        or value["format_version"] != "1.0"
        or value["service"] != "appworld"
        or value["api_url"] != "http://appworld:8080"
        or not isinstance(value["source_digest"], str)
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", value["source_digest"])
        or not isinstance(value["groups"], list)
        or not 1 <= len(value["groups"]) <= 32
    ):
        raise ValueError("Native MCP configuration is not an installed public connection")
    apps = []
    for index, group in enumerate(value["groups"]):
        if (
            not isinstance(group, dict)
            or set(group) != {"id", "apps", "tool_count"}
            or group["id"] != "appworld" + str(index)
            or type(group["tool_count"]) is not int
            or not 1 <= group["tool_count"] <= 256
            or not isinstance(group["apps"], list)
            or not 1 <= len(group["apps"]) <= 32
            or any(
                not isinstance(app, str)
                or not re.fullmatch(r"[a-z_]+", app)
                or app in {"admin", "api_docs"}
                for app in group["apps"]
            )
        ):
            raise ValueError("Native MCP app group is invalid")
        apps.extend(group["apps"])
    if len(set(apps)) != len(apps):
        raise ValueError("Native MCP app groups overlap")
    return value


def main():
    try:
        configuration = read_configuration()
        if len(sys.argv) != 2:
            raise ValueError("Native MCP requires one installed group identity")
        selected = next(group for group in configuration["groups"] if group["id"] == sys.argv[1])
        # A harness may clear PYTHONPATH and other inherited environment variables.
        sys.path.insert(0, "/opt/native-package")
        sys.path.insert(0, "/opt/benchmark-sdk")
        os.environ.update(
            {
                "PYTHON_DOTENV_DISABLED": "1",
                "APPWORLD_ROOT": "/appworld-public",
                "APPWORLD_CACHE": "/tmp/appworld-mcp-cache",
            }
        )
        from evalhive_appworld.native import verified_code

        verified_code()
        from appworld.serve._mcp import DEFAULT_APP_NAMES, run

        actual = {app for group in configuration["groups"] for app in group["apps"]}
        if actual != set(DEFAULT_APP_NAMES):
            raise ValueError("Native MCP configuration changed the official public app set")
        # The CLI requires a dataset installation; this official server API does not.
        run("stdio", app_names=selected["apps"], remote_apis_url=configuration["api_url"])
    except Exception:
        sys.stderr.write("Native AppWorld MCP initialization failed\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
