"""Retired callbacks cannot mutate historical results or start paid review."""

import uuid
from unittest.mock import AsyncMock

import pytest
from app.auth.tokens import issue_agent_token
from app.models.event import AgentEvent
from app.models.task import Task
from sqlalchemy import func, select


@pytest.mark.parametrize("authorization", [None, "Bearer unknown"])
async def test_retired_callback_still_requires_scoped_authentication(client, authorization):
    headers = {"Authorization": authorization} if authorization else {}
    response = await client.post("/api/v1/agent-webhook/00000000-0000-0000-0000-000000000000",
                                 headers=headers, json={"event": "completed"})
    assert response.status_code == 401


@pytest.mark.parametrize("prefix", ["/api/agent-webhook", "/api/v1/agent-webhook"])
async def test_repeated_retired_callback_preserves_history(auth_client, db_session, monkeypatch, prefix):
    workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    task = Task(workspace_id=workspace, title="Historical result", status="done", result_summary="Original answer")
    db_session.add(task)
    await db_session.flush()
    token = await issue_agent_token(db_session, task_id=task.id, workspace_id=workspace)
    await db_session.commit()
    review = AsyncMock(side_effect=AssertionError("Retired callbacks must not evaluate"))
    monkeypatch.setattr("app.orchestrator.llm.evaluate_agent_result", review)
    for _ in range(2):
        response = await auth_client.post(f"{prefix}/{task.id}", headers={"Authorization": f"Bearer {token}"},
                                         json={"event": "completed", "data": {"result_summary": "Replacement"}})
        assert response.status_code == 410 and response.headers["Deprecation"] == "true"
    await db_session.refresh(task)
    assert task.status == "done" and task.result_summary == "Original answer"
    assert await db_session.scalar(select(func.count()).select_from(AgentEvent).where(AgentEvent.task_id == task.id)) == 0
    review.assert_not_called()
