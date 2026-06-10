"""Integration tests for GET /api/tasks/{task_id}/decomposition (U-05)."""

import uuid
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.models.event import AgentEvent
from app.models.task import Task, TaskStatus
from app.models.template import Template


async def _create_template(db_session, workspace_id, name="Researcher"):
    t = Template(
        name=name,
        description=f"{name} template",
        soul_md="# soul",
        tool_ids=[],
        tags=[],
        workspace_id=workspace_id,
    )
    db_session.add(t)
    await db_session.flush()
    return t


async def _create_task(db_session, workspace_id, **kw):
    task = Task(
        title=kw.get("title", "task"),
        description=kw.get("description"),
        status=kw.get("status", TaskStatus.READY.value),
        workspace_id=workspace_id,
        parent_id=kw.get("parent_id"),
        template_id=kw.get("template_id"),
        retry_count=kw.get("retry_count", 0),
        max_retries=kw.get("max_retries", 1),
        depends_on=kw.get("depends_on", []),
        started_at=kw.get("started_at"),
        completed_at=kw.get("completed_at"),
    )
    db_session.add(task)
    await db_session.flush()
    return task


async def _add_event(db_session, *, task_id, container_id, event_type, workspace_id, data=None, when=None):
    ev = AgentEvent(
        task_id=task_id,
        agent_container_id=container_id,
        event_type=event_type,
        source="agent" if event_type != "agent_spawned" else "orchestrator",
        data=data or {},
        workspace_id=workspace_id,
    )
    db_session.add(ev)
    await db_session.flush()
    if when is not None:
        await db_session.execute(
            text("UPDATE agent_events SET created_at = :w WHERE id = :id"),
            {"w": when, "id": ev.id},
        )
    return ev


@pytest.mark.asyncio
async def test_decomposition_404_for_other_workspace(client: AsyncClient):
    # User A creates a task in their workspace.
    ra = await client.post(
        "/api/auth/register",
        json={"email": f"a-{uuid.uuid4().hex[:8]}@x.dev", "password": "password1234", "display_name": "A"},
    )
    pa = ra.json()
    headers_a = {"Authorization": f"Bearer {pa['access_token']}", "X-Workspace-Id": pa["default_workspace_id"]}
    create = await client.post(
        "/api/tasks", json={"title": "p", "priority": "low"}, headers=headers_a
    )
    task_id = create.json()["id"]

    # User B (different workspace) gets 404.
    rb = await client.post(
        "/api/auth/register",
        json={"email": f"b-{uuid.uuid4().hex[:8]}@x.dev", "password": "password1234", "display_name": "B"},
    )
    pb = rb.json()
    headers_b = {"Authorization": f"Bearer {pb['access_token']}", "X-Workspace-Id": pb["default_workspace_id"]}
    r = await client.get(f"/api/tasks/{task_id}/decomposition", headers=headers_b)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_decomposition_empty_for_task_without_subtasks(auth_client: AsyncClient):
    create = await auth_client.post(
        "/api/tasks", json={"title": "lonely parent", "priority": "low"}
    )
    task_id = create.json()["id"]

    r = await auth_client.get(f"/api/tasks/{task_id}/decomposition")
    assert r.status_code == 200
    body = r.json()
    assert body["parent"]["id"] == task_id
    assert body["parent"]["title"] == "lonely parent"
    assert body["subtasks"] == []


@pytest.mark.asyncio
async def test_decomposition_full_tree_with_template_name(auth_client: AsyncClient, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _create_template(db_session, workspace_id, name="Researcher")

    parent = await _create_task(db_session, workspace_id, title="root", status=TaskStatus.IN_PROGRESS.value)
    child1 = await _create_task(
        db_session, workspace_id,
        title="search",
        parent_id=parent.id,
        template_id=tpl.id,
        status=TaskStatus.DONE.value,
        depends_on=[],
    )
    child2 = await _create_task(
        db_session, workspace_id,
        title="write",
        parent_id=parent.id,
        status=TaskStatus.READY.value,
        depends_on=[child1.id],
    )
    await db_session.commit()

    r = await auth_client.get(f"/api/tasks/{parent.id}/decomposition")
    assert r.status_code == 200
    body = r.json()

    assert body["parent"]["title"] == "root"
    assert len(body["subtasks"]) == 2

    by_title = {s["title"]: s for s in body["subtasks"]}
    assert by_title["search"]["template_name"] == "Researcher"
    assert by_title["search"]["depends_on"] == []
    assert by_title["write"]["template_name"] is None
    assert by_title["write"]["depends_on"] == [str(child1.id)]


@pytest.mark.asyncio
async def test_decomposition_groups_attempts_by_container(auth_client: AsyncClient, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    parent = await _create_task(db_session, workspace_id, title="root")
    child = await _create_task(db_session, workspace_id, title="leaf", parent_id=parent.id, retry_count=2)

    base = datetime(2026, 5, 8, 12, 0, 0)
    # First container: spawned, failed.
    await _add_event(db_session, task_id=child.id, container_id="c1",
                     event_type="agent_spawned", workspace_id=workspace_id, when=base)
    await _add_event(db_session, task_id=child.id, container_id="c1",
                     event_type="agent_failed", workspace_id=workspace_id,
                     data={"error": "boom"}, when=base + timedelta(seconds=10))
    # Second container: spawned, completed.
    await _add_event(db_session, task_id=child.id, container_id="c2",
                     event_type="agent_spawned", workspace_id=workspace_id, when=base + timedelta(seconds=20))
    await _add_event(db_session, task_id=child.id, container_id="c2",
                     event_type="agent_completed", workspace_id=workspace_id, when=base + timedelta(seconds=30))
    # Third container: spawned only — still running.
    await _add_event(db_session, task_id=child.id, container_id="c3",
                     event_type="agent_spawned", workspace_id=workspace_id, when=base + timedelta(seconds=40))
    # Noise: agent_progress should be ignored.
    await _add_event(db_session, task_id=child.id, container_id="c1",
                     event_type="agent_progress", workspace_id=workspace_id,
                     data={"iteration": 1}, when=base + timedelta(seconds=5))
    await db_session.commit()

    r = await auth_client.get(f"/api/tasks/{parent.id}/decomposition")
    assert r.status_code == 200
    leaf = r.json()["subtasks"][0]
    assert leaf["title"] == "leaf"
    attempts = leaf["attempts"]
    assert len(attempts) == 3

    # Sorted by spawned_at.
    assert [a["agent_container_id"] for a in attempts] == ["c1", "c2", "c3"]
    assert attempts[0]["outcome"] == "failed"
    assert attempts[0]["error"] == "boom"
    assert attempts[1]["outcome"] == "completed"
    assert attempts[1]["error"] is None
    assert attempts[2]["outcome"] == "running"
    assert attempts[2]["finished_at"] is None


@pytest.mark.asyncio
async def test_decomposition_aborted_outcome_uses_reason(auth_client: AsyncClient, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    parent = await _create_task(db_session, workspace_id, title="root")
    child = await _create_task(db_session, workspace_id, title="aborted-one", parent_id=parent.id)

    base = datetime(2026, 5, 8, 13, 0, 0)
    await _add_event(db_session, task_id=child.id, container_id="cA",
                     event_type="agent_spawned", workspace_id=workspace_id, when=base)
    await _add_event(db_session, task_id=child.id, container_id="cA",
                     event_type="agent_aborted", workspace_id=workspace_id,
                     data={"reason": "user requested"}, when=base + timedelta(seconds=5))
    await db_session.commit()

    r = await auth_client.get(f"/api/tasks/{parent.id}/decomposition")
    body = r.json()
    attempts = body["subtasks"][0]["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "aborted"
    assert attempts[0]["error"] == "user requested"


@pytest.mark.asyncio
async def test_decomposition_ignores_events_without_container_id(auth_client: AsyncClient, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    parent = await _create_task(db_session, workspace_id, title="root")
    child = await _create_task(db_session, workspace_id, title="c", parent_id=parent.id)

    # task_retry has no container_id and must not crash / produce attempts.
    await _add_event(db_session, task_id=child.id, container_id=None,
                     event_type="task_retry", workspace_id=workspace_id, data={"reason": "x"})
    await db_session.commit()

    r = await auth_client.get(f"/api/tasks/{parent.id}/decomposition")
    assert r.status_code == 200
    assert r.json()["subtasks"][0]["attempts"] == []
