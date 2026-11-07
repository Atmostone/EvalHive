"""Keep native restore logic while transporting its CLI calls to an owned PG service."""

from __future__ import annotations

import importlib
import subprocess
from contextlib import contextmanager

from .assets import POSTGRES_DATABASES
from .native import NativeConfigurationError, native_code


@contextmanager
def restore_transport(effect):
    native_code()
    module = importlib.import_module("src.mcp_services.postgres.postgres_state_manager")
    original = module.subprocess
    restores = []

    class Transport:
        def __getattr__(self, name):
            return getattr(original, name)

        def run(self, argv, *args, **kwargs):
            if not isinstance(argv, list) or not argv or argv[0] != "pg_restore":
                return original.run(argv, *args, **kwargs)
            if (
                args
                or set(kwargs) != {"env", "capture_output", "text"}
                or not kwargs["capture_output"]
                or not kwargs["text"]
            ):
                raise NativeConfigurationError("Native PostgreSQL restore interface changed")
            result = effect("postgres.restore", {"argv": argv, "timeout_seconds": 300})
            database = argv[argv.index("-d") + 1]
            restores.append({"database": database, "exit_code": result["exit_code"]})
            return subprocess.CompletedProcess(
                argv, result["exit_code"], result["stdout"], result["stderr"]
            )

    module.subprocess = Transport()
    try:
        yield restores
        # Upstream logs failed restores and continues. A fresh installation
        # must prove that every original template was restored successfully.
        if (
            len(restores) != len(POSTGRES_DATABASES)
            or {item["database"] for item in restores} != set(POSTGRES_DATABASES)
            or any(item["exit_code"] for item in restores)
        ):
            raise NativeConfigurationError("Native PostgreSQL baseline restore was incomplete")
    finally:
        module.subprocess = original
