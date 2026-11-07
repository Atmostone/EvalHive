"""An owned Unix listener; model transport never opens a host TCP port."""

from __future__ import annotations

import asyncio
import os
import socket
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import uvicorn

from app.runtime.managed_spec import ManagedRuntimeError
from app.runtime.spec import BindMount


class _OwnedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # This server is a child service of the runner, not the API process owner.
        yield


class _OwnedConfig(uvicorn.Config):
    def configure_logging(self):
        # An embedded listener must not change the API server's global loggers.
        pass


class ModelSocket:
    """One non-reusable listener in the admitted environment's owned data tree."""

    target = "/evalhive-control"

    def __init__(self, app, scope, policy):
        self.app, self.scope, self.policy = app, scope, policy
        relative = PurePosixPath("benchmark-sessions") / scope.session_id / "model-control"
        self.directory = Path(policy.data.local) / relative
        self.host_directory = str(PurePosixPath(policy.data.host) / relative)
        self.socket_path = self.directory / "model.sock"
        self.task_path = self.target + "/model.sock"
        self.server, self.job, self.listener = None, None, None
        self.directory_fd, self.identity = None, None
        self.directory_owned = False
        self.used = False

    @property
    def mount(self):
        return BindMount(self.host_directory, self.target, True)

    async def start(self):
        if self.used:
            raise ManagedRuntimeError("A model gateway cannot reuse an uncertain listener")
        self.used = True
        if self.directory.resolve() != self.directory:
            raise ManagedRuntimeError("A model gateway cannot traverse directory links")
        # Only this control directory is mounted. Different container UIDs must
        # traverse it; the role token authorizes requests to its sole socket.
        self.directory.mkdir(parents=True, mode=0o711, exist_ok=False)
        self.directory_owned = True
        try:
            self.directory_fd = os.open(
                self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            # Linux pathname sockets have a short address limit. The directory
            # descriptor supports long owned data roots without changing cwd.
            address = f"/proc/self/fd/{self.directory_fd}/model.sock"
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(address)
            self.identity = self.socket_path.lstat().st_ino
            self.socket_path.chmod(0o666)  # Mounted containers may use different rootless UIDs.
            self.listener.listen(8)
            self.listener.setblocking(False)
            config = _OwnedConfig(
                self.app,
                loop="asyncio",
                http="h11",
                ws="none",
                lifespan="off",
                log_config=None,
                log_level=None,
                access_log=False,
                proxy_headers=False,
                server_header=False,
                date_header=False,
                timeout_keep_alive=2,
                timeout_graceful_shutdown=3,
                limit_concurrency=8,
                h11_max_incomplete_event_size=16384,
            )
            self.server = _OwnedServer(config)
            self.job = asyncio.create_task(self.server.serve(sockets=[self.listener]))
            async with asyncio.timeout(5):
                while not self.server.started:
                    if self.job.done():
                        await self.job
                        raise ManagedRuntimeError("Model gateway listener did not start")
                    await asyncio.sleep(0.01)
        except BaseException:
            await self.close()
            raise
        return self

    async def close(self):
        try:
            await self.app.close()
            if self.server is not None:
                self.server.should_exit = True
            if self.job is not None:
                try:
                    async with asyncio.timeout(5):
                        await asyncio.shield(self.job)
                except TimeoutError:
                    self.job.cancel()
                    await asyncio.wait({self.job}, timeout=1)
        finally:
            if self.listener is not None:
                self.listener.close()
            try:
                if self.identity is not None and self.socket_path.exists():
                    if (
                        self.socket_path.is_symlink()
                        or self.socket_path.lstat().st_ino != self.identity
                    ):
                        raise ManagedRuntimeError(
                            "Model gateway socket identity changed before release"
                        )
                    self.socket_path.unlink()
                    self.identity = None
            finally:
                if self.directory_fd is not None:
                    os.close(self.directory_fd)
                    self.directory_fd = None
            if self.directory_owned and self.directory.exists():
                self.directory.rmdir()
                self.directory_owned = False
