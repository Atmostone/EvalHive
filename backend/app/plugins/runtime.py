"""AgentRuntime abstraction — currently only Docker. Used by orchestrator + api/agents."""

from __future__ import annotations

import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class AgentSpec:
    task_id: str
    task_description: str
    template_name: str
    template_id: str
    soul_md: str
    tools: list
    mcp_servers: list
    env: dict
    resource_limits: dict  # mem, cpu_quota
    workspace_id: str
    agent_token: str
    memory_context: str = ""
    extra_labels: dict = field(default_factory=dict)
    # Extra container env (e.g. AGENT_TOOL_INJECTION for the perturbation judge).
    extra_env: dict = field(default_factory=dict)


class AgentRuntime(ABC):
    @abstractmethod
    def spawn(self, spec: AgentSpec) -> str: ...

    @abstractmethod
    def kill(self, container_id: str, workspace_id: str | None = None) -> bool: ...

    @abstractmethod
    def list_active(self, workspace_id: str | None = None) -> list[dict]: ...

    @abstractmethod
    def kill_all(self, workspace_id: str | None = None) -> int: ...

    @abstractmethod
    def stats(self, container_id: str, workspace_id: str | None = None) -> dict | None: ...

    @abstractmethod
    async def health(self, container_id: str) -> dict | None: ...

    @abstractmethod
    async def send_command(self, container_id: str, kind: str, payload: dict) -> bool: ...


class DockerRuntime(AgentRuntime):
    """Thin adapter over the existing app.orchestrator.docker_manager helpers."""

    def spawn(self, spec: AgentSpec) -> str:
        from app.orchestrator.docker_manager import spawn_agent

        # docker_manager.spawn_agent currently accepts a Template object directly.
        # We mirror its signature here to avoid duplicating its implementation.
        from types import SimpleNamespace

        template = SimpleNamespace(
            id=uuid.UUID(spec.template_id),
            name=spec.template_name,
            soul_md=spec.soul_md,
            tools=spec.tools,
            mcp_servers=spec.mcp_servers,
            max_ram=spec.resource_limits.get("max_ram"),
            max_cpu=spec.resource_limits.get("max_cpu"),
        )
        llm_settings = {
            "llm_api_key": spec.env.get("OPENAI_API_KEY", ""),
            "llm_base_url": spec.env.get("OPENAI_BASE_URL", ""),
            "llm_model": spec.env.get("LLM_MODEL", ""),
        }
        return spawn_agent(
            task_id=spec.task_id,
            task_description=spec.task_description,
            template=template,
            llm_settings=llm_settings,
            workspace_id=spec.workspace_id,
            agent_token=spec.agent_token,
            memory_context=spec.memory_context,
            extra_env=spec.extra_env or {},
        )

    def kill(self, container_id: str, workspace_id: str | None = None) -> bool:
        from app.orchestrator.docker_manager import kill_agent

        return kill_agent(container_id, workspace_id=workspace_id)

    def list_active(self, workspace_id: str | None = None) -> list[dict]:
        from app.orchestrator.docker_manager import list_agents

        return list_agents(workspace_id=workspace_id)

    def kill_all(self, workspace_id: str | None = None) -> int:
        from app.orchestrator.docker_manager import kill_all_agents

        return kill_all_agents(workspace_id=workspace_id)

    def stats(self, container_id: str, workspace_id: str | None = None) -> dict | None:
        from app.orchestrator.docker_manager import get_agent_stats

        return get_agent_stats(container_id, workspace_id=workspace_id)

    async def health(self, container_id: str) -> dict | None:
        from app.orchestrator.docker_manager import get_agent_health

        return await get_agent_health(container_id)

    async def send_command(self, container_id: str, kind: str, payload: dict) -> bool:
        from app.orchestrator.docker_manager import (
            abort_agent,
            send_feedback,
            switch_agent_model,
        )

        if kind == "feedback":
            return await send_feedback(container_id, payload.get("message", ""))
        if kind == "switch_model":
            return await switch_agent_model(container_id, payload)
        if kind == "abort":
            return await abort_agent(container_id, payload.get("reason", ""))
        raise ValueError(f"unknown agent command: {kind}")


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
