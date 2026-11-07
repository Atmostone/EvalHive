"""Launch the installed native MCP server using a read-only prepared connection."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

CONNECTION = Path("/evalhive-connection/connection.json")
CLIENTS = Path("/opt/mcp-clients/node_modules/.bin")
MAX_FRAME = 8 * 1024**2


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
    process = await asyncio.create_subprocess_exec(
        *argv,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=sys.stderr,
        limit=MAX_FRAME + 1,
    )
    reader = asyncio.StreamReader(limit=MAX_FRAME + 1)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )

    async def requests():
        while line := await reader.readline():
            frame = fixed_roots_frame(line)
            if frame is not None:
                process.stdin.write(frame)
                await process.stdin.drain()
        process.stdin.close()

    async def responses():
        while line := await process.stdout.readline():
            if not line.endswith(b"\n") or len(line) > MAX_FRAME:
                raise ValueError("Native MCP response exceeds its byte budget")
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()

    tasks = [asyncio.create_task(requests()), asyncio.create_task(responses())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if tasks[0] in done:
            await asyncio.wait_for(tasks[1], 5)
        return await asyncio.wait_for(process.wait(), 5)
    finally:
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
    except Exception:
        sys.stderr.write("Prepared native MCP client could not start\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
