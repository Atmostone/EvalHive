"""Native Verifier environment methods over the public Benchmark SDK broker."""

from __future__ import annotations

import math
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import PurePosixPath

from evalhive_benchmark import ProtocolError
from harbor.constants import MAIN_SERVICE_NAME
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.docker.docker_unix import UnixOps
from harbor.models.task.config import TaskOS


class BrokerEnvironment:
    """The connector supplies transfers; this bridge receives no Docker access."""

    def __init__(self, context, scope, provider, handle, *, cwd, deadline, transfers, task_os=TaskOS.LINUX):
        path = PurePosixPath(cwd)
        if not path.is_absolute() or ".." in path.parts or "\0" in cwd:
            raise ValueError("Native environment requires its resolved absolute working directory")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            raise ValueError("Native environment requires a finite monotonic deadline")
        self.context, self.scope = context, deepcopy(scope)
        self.provider, self.handle = provider, handle
        self.cwd, self.deadline, self.transfers, self.os = cwd, deadline, transfers, task_os
        self.default_user, self.sequence = None, 0
        self.recording_complete = True

    @contextmanager
    def with_default_user(self, user):
        previous, self.default_user = self.default_user, user
        try:
            yield
        finally:
            self.default_user = previous

    def is_path_mounted(self, *args):
        # Worker and Task have independent filesystems, including verifier logs.
        return False

    @staticmethod
    def _service(service):
        if service not in {None, MAIN_SERVICE_NAME}:
            raise ProtocolError("NATIVE_SERVICE_UNAVAILABLE", "Native service requires its installed adapter", "unsupported")

    async def exec(self, *, command, env=None, user=None, timeout_sec=None):
        if self.os != TaskOS.LINUX:
            raise ProtocolError("NATIVE_OS_UNAVAILABLE", "Native execution requires an installed OS adapter", "unsupported")
        remaining = self.deadline - time.monotonic()
        if timeout_sec is not None:
            if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, (int, float)) or not math.isfinite(timeout_sec) or timeout_sec <= 0:
                raise ValueError("Native command timeout must be positive and finite")
            remaining = min(remaining, timeout_sec)
        if remaining <= 0:
            raise ProtocolError("NATIVE_VERIFIER_TIMEOUT", "Native environment deadline expired", "grading")
        if remaining > 86400:
            raise ProtocolError("NATIVE_TIMEOUT_UNAVAILABLE", "Native command exceeds the installed execution duration", "unsupported")
        selected_user = user if user is not None else self.default_user
        payload = {
            "argv": UnixOps.exec_shell_args(command), "cwd": self.cwd,
            "timeout_seconds": remaining, "deadline_policy": "inherit",
            **({"environment": env} if env is not None else {}),
            **({"user": str(selected_user)} if selected_user is not None else {}),
        }
        self.sequence += 1
        result = await self.context.broker(
            self.scope, self.provider, "exec", payload, handle=self.handle,
            request_id="native.environment.exec." + str(self.sequence),
            timeout=remaining + 5, durable_host=True,
        )
        if (
            type(result.get("exit_code")) is not int
            or not isinstance(result.get("stdout"), str)
            or not isinstance(result.get("stderr"), str)
        ):
            raise ProtocolError("NATIVE_EXEC_RECEIPT_INVALID", "Native execution lacks a confirmed result", "recording")
        if result.get("stdout_truncated") or result.get("stderr_truncated"):
            self.recording_complete = False
        return ExecResult(return_code=result["exit_code"], stdout=result["stdout"], stderr=result["stderr"])

    async def upload_dir(self, *, source_dir, target_dir):
        await self.transfers.upload(source_dir=source_dir, target_dir=target_dir)

    async def download_dir(self, *, source_dir, target_dir, service=None):
        self._service(service)
        await self.transfers.download(source_dir=source_dir, target_dir=target_dir)

    async def download_dir_filtered(self, *, source_dir, target_dir, include=None, exclude=None, protect=None, service=None):
        self._service(service)
        await self.transfers.download(
            source_dir=source_dir, target_dir=target_dir, include=include,
            exclude=exclude, protect=protect,
        )

    async def upload_file(self, *, source_path, target_path):
        await self.transfers.upload_file(source_path=source_path, target_path=target_path)

    async def download_file(self, *, source_path, target_path, service=None):
        self._service(service)
        await self.transfers.download_file(source_path=source_path, target_path=target_path)

    async def path_is_dir(self, path, *, service=None, user=None):
        self._service(service)
        return await self.transfers.path_is_dir(str(path))

    async def ensure_dirs(self, dirs, *, chmod=True):
        if dirs:
            return await self.exec(command=BaseEnvironment._ensure_dirs_command(self, dirs, chmod=chmod), user="root" if chmod else None)

    async def empty_dirs(self, dirs, *, chmod=True):
        if dirs:
            return await self.exec(command=BaseEnvironment._empty_dirs_command(self, dirs, chmod=chmod), user="root")
