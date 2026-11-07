"""Private Unix-socket control; no native administration endpoint is exposed on TCP."""

from __future__ import annotations

import base64
import json
import os
import socket
import socketserver
import stat
import sys
import threading
import time
from pathlib import Path

from .world_rpc import MAX_BYTES, World, WorldError, encoded

SOCKET = Path("/control/world.sock")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorldError("Native control contains duplicate fields")
        result[key] = value
    return result


def decode(data):
    if not data.endswith(b"\n") or len(data) > MAX_BYTES + 1:
        raise WorldError("Native control frame exceeds its budget")
    value = json.loads(data, object_pairs_hook=_object)
    encoded(value)
    return value


def exchange(request, path=SOCKET, *, timeout=10):
    frame = encoded(request) + b"\n"
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Native control did not become ready")
            connection.settimeout(remaining)
            try:
                connection.connect(str(path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                # A running container has not necessarily bound its application socket.
                # No request bytes have been sent, so waiting cannot repeat an effect.
                time.sleep(min(0.05, remaining))
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        connection.sendall(frame)
        with connection.makefile("rb") as stream:
            response = decode(stream.readline(MAX_BYTES + 2))
    if set(response) != {"ok", "result"} or type(response["ok"]) is not bool:
        raise WorldError("Native control receipt is invalid")
    if not response["ok"]:
        raise WorldError("Native control rejected the request")
    return response["result"]


def serve(factory, path=SOCKET, *, stop=None, deadline_seconds=86400):
    path = Path(path).absolute()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.stat()
    if (
        path.parent.resolve() != path.parent
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
        or path.exists()
        or path.is_symlink()
    ):
        raise WorldError("Native control requires an unused private socket directory")
    world = World(factory, deadline_seconds=deadline_seconds)
    stop = stop or threading.Event()

    class Handler(socketserver.StreamRequestHandler):
        timeout = 5

        def handle(self):
            try:
                result = world.request(decode(self.rfile.readline(MAX_BYTES + 2)))
                response = encoded({"ok": True, "result": result})
            except Exception:
                response = encoded({"ok": False, "result": {"code": "NATIVE_CONTROL_REJECTED"}})
            try:
                self.wfile.write(response + b"\n")
            except OSError:
                pass

    try:
        with socketserver.UnixStreamServer(str(path), Handler) as server:
            os.chmod(path, 0o600)
            server.timeout = 0.2
            while not stop.is_set():
                server.handle_request()
    finally:
        world.close()
        path.unlink(missing_ok=True)


def main():
    try:
        if len(sys.argv) != 2:
            raise WorldError("Native control requires one encoded request")
        request = json.loads(
            base64.b64decode(sys.argv[1], validate=True), object_pairs_hook=_object
        )
        result = exchange(request)
        sys.stdout.buffer.write(encoded(result) + b"\n")
    except Exception as error:
        sys.stderr.write("Native control request failed (" + type(error).__name__ + ")\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
