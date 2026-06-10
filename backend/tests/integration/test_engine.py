"""Integration tests for app.orchestrator.engine — exercises orchestrator decision flow."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.orchestrator import engine


@pytest.mark.asyncio
async def test_process_ready_task_no_orchestrator_model_marks_failed(db_session):
    """When workspace has no orchestrator_model_id, the engine must fail loudly."""
    task = Task(
        title="t",
        description="d",
        priority="low",
        status=TaskStatus.READY.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # No orchestrator model configured → engine fails before checking templates.
    await engine.process_ready_task(db_session, task)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED.value


@pytest.mark.asyncio
async def test_process_ready_task_no_templates_marks_failed(db_session, default_model):
    task = Task(
        title="t",
        description="d",
        priority="low",
        status=TaskStatus.READY.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Orchestrator model is configured (via default_model fixture) but no templates
    # exist → engine bails out with FAILED at the second guard.
    await engine.process_ready_task(db_session, task)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED.value


@pytest.mark.asyncio
async def test_process_ready_task_spawns_when_template_picked(db_session, default_model, monkeypatch):
    # Seed exactly one template so the orchestrator skips decomposition (needs >1).
    tpl = Template(
        name="solo",
        description="d",
        soul_md="# soul",
        model_id=default_model.id,
        tool_ids=[],
        max_ram="1g",
        max_cpu=100000,
        timeout_minutes=60,
        tags=[],
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(tpl)
    task = Task(
        title="t",
        description="d",
        priority="low",
        status=TaskStatus.READY.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(tpl)
    await db_session.refresh(task)

    # Patch select_template_for_task to deterministically return our template.
    async def pick(*a, **kw):
        return {"template_id": str(tpl.id), "reasoning": "only one"}

    monkeypatch.setattr("app.orchestrator.engine.select_template_for_task", pick)

    # Patch the AgentRuntime so we don't actually run Docker.
    fake_runtime = MagicMock()
    fake_runtime.spawn.return_value = "ctr-id-1234567890ab"
    monkeypatch.setattr("app.orchestrator.engine.get_agent_runtime", lambda: fake_runtime)

    # Patch issue_agent_token (would otherwise need a real DB row).
    monkeypatch.setattr(
        "app.orchestrator.engine.issue_agent_token",
        AsyncMock(return_value="fake-token"),
    )

    await engine.process_ready_task(db_session, task)
    await db_session.refresh(task)

    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.template_id == tpl.id
    assert task.agent_container_id == "ctr-id-1234567890ab"
    fake_runtime.spawn.assert_called_once()


@pytest.mark.asyncio
async def test_process_ready_task_skips_decomposition_when_disabled(
    db_session, default_model, monkeypatch
):
    """When decomposition_enabled=False, multi-template root tasks must go
    straight to single-template selection (decide_decomposition never called)."""
    from app.models.setting import Setting

    await db_session.merge(Setting(key="decomposition_enabled", value=False))

    def _tpl(name: str) -> Template:
        return Template(
            name=name, description="d", soul_md="# soul", model_id=default_model.id,
            tool_ids=[],
            max_ram="1g", max_cpu=100000, timeout_minutes=60, tags=[],
            workspace_id=DEFAULT_WORKSPACE_ID,
        )

    tpl_a, tpl_b = _tpl("a"), _tpl("b")
    db_session.add_all([tpl_a, tpl_b])
    task = Task(
        title="multi-step research", description="d", priority="low",
        status=TaskStatus.READY.value, workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(tpl_a)
    await db_session.refresh(task)

    decompose_mock = AsyncMock(return_value=[{"title": "x", "template_id": str(tpl_a.id)}])
    monkeypatch.setattr("app.orchestrator.engine.decide_decomposition", decompose_mock)

    async def pick(*a, **kw):
        return {"template_id": str(tpl_a.id), "reasoning": "single agent path"}

    monkeypatch.setattr("app.orchestrator.engine.select_template_for_task", pick)
    fake_runtime = MagicMock()
    fake_runtime.spawn.return_value = "ctr-disabled-deco"
    monkeypatch.setattr("app.orchestrator.engine.get_agent_runtime", lambda: fake_runtime)
    monkeypatch.setattr(
        "app.orchestrator.engine.issue_agent_token",
        AsyncMock(return_value="fake-token"),
    )

    await engine.process_ready_task(db_session, task)
    await db_session.refresh(task)

    decompose_mock.assert_not_called()
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.template_id == tpl_a.id
    assert task.agent_container_id == "ctr-disabled-deco"
    subs = (await db_session.execute(select(Task).where(Task.parent_id == task.id))).scalars().all()
    assert subs == []


@pytest.mark.asyncio
async def test_check_parent_task_completion_marks_parent_done(db_session):
    parent = Task(
        title="p", priority="medium", status=TaskStatus.IN_PROGRESS.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    sub = Task(
        title="s", parent_id=parent.id, priority="low",
        status=TaskStatus.DONE.value, workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(sub)
    await db_session.commit()
    await db_session.refresh(sub)

    await engine.check_parent_task_completion(db_session, sub)
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.DONE.value
    assert parent.completed_at is not None


@pytest.mark.asyncio
async def test_check_parent_task_completion_marks_parent_failed_when_any_subtask_failed(db_session):
    parent = Task(
        title="p", priority="medium", status=TaskStatus.IN_PROGRESS.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    sub_ok = Task(
        title="s1", parent_id=parent.id, priority="low",
        status=TaskStatus.DONE.value, workspace_id=DEFAULT_WORKSPACE_ID,
    )
    sub_bad = Task(
        title="s2", parent_id=parent.id, priority="low",
        status=TaskStatus.FAILED.value, workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add_all([sub_ok, sub_bad])
    await db_session.commit()
    await db_session.refresh(sub_bad)

    await engine.check_parent_task_completion(db_session, sub_bad)
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.FAILED.value


@pytest.mark.asyncio
async def test_check_parent_task_completion_no_op_when_some_pending(db_session):
    parent = Task(
        title="p", priority="medium", status=TaskStatus.IN_PROGRESS.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    sub_done = Task(
        title="s1", parent_id=parent.id, priority="low",
        status=TaskStatus.DONE.value, workspace_id=DEFAULT_WORKSPACE_ID,
    )
    sub_running = Task(
        title="s2", parent_id=parent.id, priority="low",
        status=TaskStatus.IN_PROGRESS.value, workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add_all([sub_done, sub_running])
    await db_session.commit()
    await db_session.refresh(sub_done)

    original_status = parent.status
    await engine.check_parent_task_completion(db_session, sub_done)
    await db_session.refresh(parent)
    assert parent.status == original_status  # unchanged


@pytest.mark.asyncio
async def test_check_parent_task_completion_skips_when_no_parent(db_session):
    orphan = Task(
        title="o", priority="low", status=TaskStatus.DONE.value,
        workspace_id=DEFAULT_WORKSPACE_ID, parent_id=None,
    )
    db_session.add(orphan)
    await db_session.commit()
    # Should not raise.
    await engine.check_parent_task_completion(db_session, orphan)
