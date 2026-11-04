"""Deprecated agent-runtime compatibility exports; new code uses app.runtime."""

import os

from app.harnesses.legacy_runtime import AgentRuntime, AgentSpec, DockerRuntime

__all__ = ["AgentRuntime", "AgentSpec", "DockerRuntime", "get_agent_runtime", "set_agent_runtime"]

_runtime: AgentRuntime | None = None


def get_agent_runtime() -> AgentRuntime:
    global _runtime
    if _runtime is not None:
        return _runtime
    name = os.environ.get("AGENT_RUNTIME", "docker")
    if name == "docker":
        _runtime = DockerRuntime()
    else:
        raise ValueError(f"unknown AGENT_RUNTIME={name}")
    return _runtime


def set_agent_runtime(runtime: AgentRuntime | None) -> None:
    global _runtime
    _runtime = runtime
