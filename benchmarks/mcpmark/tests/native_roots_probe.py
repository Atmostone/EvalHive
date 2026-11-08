"""Run inside the task image against its pinned, unmodified Filesystem MCP server."""

import json
import signal
import subprocess
import sys
from pathlib import Path


def main():
    root = Path("/opt/native/.mcpmark_backups/backup_roots_probe")
    root.mkdir(parents=True)
    (root / "input.txt").write_text("native initial input")
    connection = Path("/evalhive-connection")
    connection.mkdir()
    (connection / "connection.json").write_text(
        json.dumps({"service": "filesystem", "config": {"test_directory": str(root)}})
    )

    def timeout(*args):
        raise TimeoutError("Native MCP roots probe expired")

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(20)
    process = subprocess.Popen(
        [sys.executable, "/opt/environment/task_client.py"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def send(value):
        process.stdin.write(json.dumps({"jsonrpc": "2.0", **value}) + "\n")
        process.stdin.flush()

    def response(identity):
        while True:
            line = process.stdout.readline()
            assert line, "Native MCP server closed before responding"
            value = json.loads(line)
            assert value.get("method") != "roots/list", "The native root must remain fixed"
            if value.get("id") == identity:
                return value["result"]

    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"roots": {"listChanged": True}},
                    "clientInfo": {"name": "workspace-roots-probe", "version": "1"},
                },
            }
        )
        response(1)
        send({"method": "notifications/initialized"})
        send({"method": "notifications/roots/list_changed"})
        send(
            {
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_allowed_directories", "arguments": {}},
            }
        )
        allowed = response(2)
        assert str(root) in json.dumps(allowed) and "/workspace" not in json.dumps(allowed), allowed
        send(
            {
                "id": 3,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "/workspace/outside.txt"}},
            }
        )
        assert response(3)["isError"] is True
        print("Native MCP roots stayed inside the prepared filesystem boundary")
    finally:
        signal.alarm(0)
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    main()
