"""Private Unix-socket server, independent of simulated native clocks."""

from __future__ import annotations

import os
import selectors
import socketserver
import stat
import threading
from pathlib import Path

from .world_rpc import World
from .world_wire import MAX_BYTES, SOCKET, WorldError, decode, encoded, exchange, main  # noqa: F401


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
        with (
            socketserver.UnixStreamServer(str(path), Handler) as server,
            selectors.DefaultSelector() as ready,
        ):
            os.chmod(path, 0o600)
            ready.register(server, selectors.EVENT_READ)
            while not stop.is_set():
                # BaseServer.handle_request calculates deadlines with a Python clock
                # that native simulated time can replace or restore mid-request.
                if ready.select(0.2):
                    server._handle_request_noblock()
    finally:
        world.close()
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
