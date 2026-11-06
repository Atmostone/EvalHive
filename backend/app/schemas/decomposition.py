from typing import Literal

from pydantic import BaseModel


class AgentAttempt(BaseModel):
    agent_container_id: str
    spawned_at: str
    finished_at: str | None = None
    outcome: Literal["completed", "failed", "aborted", "running"]
    error: str | None = None


class DecompositionSubtask(BaseModel):
    id: str
    title: str
    template_name: str | None = None
    status: str
    retry_count: int
    max_retries: int
    depends_on: list[str]
    started_at: str | None = None
    completed_at: str | None = None
    cost_usd: float | None
    result_files_count: int
    attempts: list[AgentAttempt]


class DecompositionParent(BaseModel):
    id: str
    title: str
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    cost_usd: float | None


class DecompositionResponse(BaseModel):
    parent: DecompositionParent
    subtasks: list[DecompositionSubtask]
