"""Launch the installed native MCP server using a read-only prepared connection."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

CONNECTION = Path("/evalhive-connection/connection.json")
CLIENTS = Path("/opt/mcp-clients/node_modules/.bin")
MAX_FRAME = 8 * 1024**2
DIAGNOSTICS = Path("/tmp/evalhive-mcpmark-client.jsonl")


def trace(event, **fields):
    """Record bounded process states only, never protocol bodies or credentials."""
    try:
        descriptor = os.open(
            DIAGNOSTICS,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        with os.fdopen(descriptor, "ab") as stream:
            info = os.fstat(stream.fileno())
            if stat.S_ISREG(info.st_mode) and info.st_size < 64 * 1024:
                stream.write(
                    (
                        json.dumps(
                            {"pid": os.getpid(), "time": time.monotonic(), "event": event, **fields}
                        )
                        + "\n"
                    ).encode()
                )
    except OSError:
        pass


def fixed_roots_frame(line):
    if not line.endswith(b"\n") or len(line) > MAX_FRAME:
        raise ValueError("Native MCP frame exceeds its byte budget")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("Native MCP frame is invalid")
    if value.get("method") == "notifications/roots/list_changed":
        return None
    if value.get("method") == "initialize":
        capabilities = value["params"].get("capabilities", {})
        if not isinstance(capabilities, dict):
            raise ValueError("Native MCP capabilities are invalid")
        # The filesystem server otherwise replaces its explicit CLI directory
        # with a Harness's workspace roots. Keep the prepared native boundary.
        value["params"]["capabilities"] = {
            key: item for key, item in capabilities.items() if key != "roots"
        }
        return (json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    return line


async def filesystem_proxy(argv, environment):
    trace("proxy.start")
    process = await asyncio.create_subprocess_exec(
        *argv,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=sys.stderr,
        limit=MAX_FRAME + 1,
    )
    reader = asyncio.StreamReader(limit=MAX_FRAME + 1)
    trace("server.spawned", child_pid=process.pid)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )

    async def requests():
        while line := await reader.readline():
            frame = fixed_roots_frame(line)
            if frame is not None:
                if json.loads(line).get("method") == "initialize":
                    trace("client.initialize")
                process.stdin.write(frame)
                await process.stdin.drain()
        trace("client.closed")
        process.stdin.close()

    async def responses():
        first = True
        while line := await process.stdout.readline():
            if not line.endswith(b"\n") or len(line) > MAX_FRAME:
                raise ValueError("Native MCP response exceeds its byte budget")
            if first:
                trace("server.responded")
                first = False
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
        trace("server.closed", exit_code=process.returncode)

    tasks = [asyncio.create_task(requests()), asyncio.create_task(responses())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if tasks[0] in done:
            await asyncio.wait_for(tasks[1], 5)
        return await asyncio.wait_for(process.wait(), 5)
    except Exception as error:
        trace("proxy.error", exception=type(error).__name__)
        raise
    finally:
        trace("proxy.stopping", exit_code=process.returncode)
        transport.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 1)
            except TimeoutError:
                process.kill()
                await process.wait()


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
    if service == "postgres":
        if set(config) != {
            "host",
            "port",
            "username",
            "password",
            "current_database",
            "database_url",
        }:
            raise ValueError("Prepared PostgreSQL settings are invalid")
        if (
            config["host"] != "postgres"
            or config["port"] != 5432
            or config["username"] != "postgres"
        ):
            raise ValueError("Prepared PostgreSQL is outside its owned service")
        if not all(
            isinstance(config[name], str) and config[name]
            for name in ("password", "current_database")
        ):
            raise ValueError("Prepared PostgreSQL credentials or database are unavailable")
        uri = (
            "postgresql://postgres:"
            + quote(config["password"], safe="")
            + "@postgres:5432/"
            + quote(config["current_database"], safe="")
        )
        return ["/opt/postgres-client/bin/postgres-mcp", "--access-mode=unrestricted"], {
            "DATABASE_URI": uri
        }
    if service in {"github", "notion"}:
        field = "github_token" if service == "github" else "notion_key"
        if (
            set(config) != {field}
            or not isinstance(config[field], str)
            or not config[field]
            or "\0" in config[field]
        ):
            raise ValueError("Prepared native external credentials are invalid")
        if service == "github":
            return ["/opt/github-mcp-server/github-mcp-server", "stdio"], {
                "GITHUB_PERSONAL_ACCESS_TOKEN": config[field]
            }
        return [str(CLIENTS / "notion-mcp-server")], {
            "OPENAPI_MCP_HEADERS": json.dumps(
                {"Authorization": "Bearer " + config[field], "Notion-Version": "2022-06-28"}
            )
        }
    if service in {"playwright", "playwright_webarena"}:
        allowed = (
            {"browser", "headless", "browser_state", "test_environments"}
            if service == "playwright"
            else {"environment", "base_url", "docker"}
        )
        if set(config) - allowed:
            raise ValueError("Prepared native browser configuration is invalid")
        browser = config.get("browser", "chromium")
        if browser not in {"chromium", "firefox"} or config.get("headless", True) is not True:
            raise ValueError("Native browser requires a supported headless engine")
        if service == "playwright_webarena":
            endpoint = urlsplit(config.get("base_url", ""))
            if (
                endpoint.scheme != "http"
                or endpoint.hostname != "localhost"
                or endpoint.port != 80
                or endpoint.username is not None
                or endpoint.password is not None
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError("Native WebArena connection is outside its owned namespace")
        return [
            str(CLIENTS / "playwright-mcp"),
            "--headless",
            "--isolated",
            "--no-sandbox",
            "--browser",
            browser,
            "--viewport-size",
            "1280,720",
        ], {}
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
        connection = json.loads(data)
        argv, private_env = launch_config(connection)
        if connection["service"] == "filesystem":
            code = asyncio.run(filesystem_proxy(argv, {**os.environ, **private_env}))
            raise SystemExit(code)
        os.execve(argv[0], argv, {**os.environ, **private_env})
    except Exception as error:
        trace("client.failed", exception=type(error).__name__)
        sys.stderr.write("Prepared native MCP client could not start\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
