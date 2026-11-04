"""Harness-neutral process runtime. Connector configuration belongs elsewhere."""

from app.runtime.base import ProcessRuntime

_runtime: ProcessRuntime | None = None


def get_process_runtime() -> ProcessRuntime:
    global _runtime
    if _runtime is None:
        from app.runtime.docker import DockerProcessRuntime

        _runtime = DockerProcessRuntime()
    return _runtime


def set_process_runtime(runtime: ProcessRuntime | None) -> None:
    global _runtime
    _runtime = runtime
