"""Tests for chat slash-command parsing — invokes handle_slash_command directly."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.api import chat
from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.plugins import runtime as rt


class _NoopRuntime:
    def list_active(self, workspace_id=None):
        return []
    def kill(self, cid, workspace_id=None):
        return True
    def kill_all(self, workspace_id=None):
        return 0


@pytest.fixture(autouse=True)
def _noop_runtime():
    rt.set_agent_runtime(_NoopRuntime())
    yield
    rt.set_agent_runtime(None)


@pytest.mark.asyncio
async def test_help_command(db_session):
    out = await chat.handle_slash_command(db_session, "/help", DEFAULT_WORKSPACE_ID)
    assert "/status" in out
    assert "/kill" in out


@pytest.mark.asyncio
async def test_unknown_command(db_session):
    out = await chat.handle_slash_command(db_session, "/foobar", DEFAULT_WORKSPACE_ID)
    assert "Unknown command" in out


@pytest.mark.asyncio
async def test_status_command_runs(db_session):
    out = await chat.handle_slash_command(db_session, "/status", DEFAULT_WORKSPACE_ID)
    assert "Active agents" in out
    assert "0" in out


@pytest.mark.asyncio
async def test_kill_all_command(db_session):
    out = await chat.handle_slash_command(db_session, "/kill all", DEFAULT_WORKSPACE_ID)
    assert "Killed 0 container" in out


@pytest.mark.asyncio
async def test_kill_specific_not_found(db_session):
    class _RT(_NoopRuntime):
        def kill(self, cid, workspace_id=None):
            return False
    rt.set_agent_runtime(_RT())
    out = await chat.handle_slash_command(db_session, "/kill abc", DEFAULT_WORKSPACE_ID)
    assert "not found" in out
    rt.set_agent_runtime(_NoopRuntime())


@pytest.mark.asyncio
async def test_kill_usage_when_no_target(db_session):
    out = await chat.handle_slash_command(db_session, "/kill", DEFAULT_WORKSPACE_ID)
    assert "Usage" in out


@pytest.mark.asyncio
async def test_spawn_command_with_existing_template(db_session):
    tpl = Template(
        name="alpha",
        description="d",
        soul_md="# soul",
        tool_ids=[],
        max_ram="1g", max_cpu=100000, timeout_minutes=60, tags=[],
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(tpl)
    await db_session.commit()

    out = await chat.handle_slash_command(
        db_session, '/spawn alpha "first task"', DEFAULT_WORKSPACE_ID
    )
    assert "spawned" in out
    assert "alpha" in out


@pytest.mark.asyncio
async def test_spawn_command_unknown_template(db_session):
    out = await chat.handle_slash_command(
        db_session, '/spawn missingone "x"', DEFAULT_WORKSPACE_ID
    )
    assert "not found" in out


@pytest.mark.asyncio
async def test_spawn_command_bad_format(db_session):
    out = await chat.handle_slash_command(db_session, "/spawn bad", DEFAULT_WORKSPACE_ID)
    assert "Usage" in out


@pytest.mark.asyncio
async def test_board_command(db_session):
    out = await chat.handle_slash_command(db_session, "/board", DEFAULT_WORKSPACE_ID)
    assert "/tasks" in out


@pytest.mark.asyncio
async def test_templates_command_lists_workspace_templates(db_session):
    db_session.add(Template(
        name="zeta", description="d", soul_md="# s",
        tool_ids=[],
        max_ram="1g", max_cpu=100000, timeout_minutes=60, tags=["primary"],
        workspace_id=DEFAULT_WORKSPACE_ID,
    ))
    await db_session.commit()

    out = await chat.handle_slash_command(db_session, "/templates", DEFAULT_WORKSPACE_ID)
    assert "zeta" in out
    assert "primary" in out


@pytest.mark.asyncio
async def test_tasks_command_lists_recent(db_session):
    t = Task(title="recent task", priority="low", status=TaskStatus.READY.value,
             workspace_id=DEFAULT_WORKSPACE_ID)
    db_session.add(t)
    await db_session.commit()
    out = await chat.handle_slash_command(db_session, "/tasks", DEFAULT_WORKSPACE_ID)
    assert "recent task" in out


@pytest.mark.asyncio
async def test_tasks_command_empty(db_session):
    out = await chat.handle_slash_command(db_session, "/tasks", DEFAULT_WORKSPACE_ID)
    assert "No tasks yet" in out
