"""Integration tests for the Trace Cleaner preview endpoint (E-06).

GET /api/quality/records/{task_id}/trace cleans a task's real trajectory
(events + log chunks): it drops the system snapshot and noise events, truncates
long tool outputs, reports token savings, honours the keep_tail_on_error flag,
and is workspace-scoped.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient

from app import database
from app.models.agent_log import AgentLogChunk
from app.models.event import AgentEvent
from app.models.task import Task, TaskStatus
from app.models.workspace import DEFAULT_WORKSPACE_ID

_BASE = datetime(2026, 1, 1, 12, 0, 0)


async def _make_task(ws, **kw):
    kw.setdefault("result_summary", "result")
    async with database.async_session() as s:
        t = Task(title="t", status=TaskStatus.DONE.value, workspace_id=ws, model_used="m", **kw)
        s.add(t)
        await s.commit()
        return str(t.id), t.id


async def _seed_trajectory(ws, task_id, *, tool_content):
    async with database.async_session() as s:
        s.add(AgentEvent(task_id=task_id, workspace_id=ws, event_type="agent_spawned",
                         source="orchestrator", data={"soul_md": "SYSTEM " * 2000, "tools": ["a"]},
                         created_at=_BASE))
        s.add(AgentEvent(task_id=task_id, workspace_id=ws, event_type="agent_health",
                         source="system", data={"status": "ok"}, created_at=_BASE + timedelta(seconds=1)))
        s.add(AgentEvent(task_id=task_id, workspace_id=ws, event_type="orchestrator_reasoning",
                         source="orchestrator", data={"reasoning": "pick the analytical template"},
                         created_at=_BASE + timedelta(seconds=2)))
        s.add(AgentLogChunk(task_id=task_id, workspace_id=ws, chunk_seq=0,
                            content=tool_content, tool_name="web_search"))
        await s.commit()


@pytest.mark.asyncio
async def test_trace_drops_snapshot_and_measures_savings(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid, task_id = await _make_task(ws)
    await _seed_trajectory(ws, task_id, tool_content="result line " * 400)

    r = await auth_client.get(f"/api/quality/records/{tid}/trace?tool_output_token_cap=50")
    assert r.status_code == 200, r.text
    trace = r.json()["cleaned_trace"]

    kinds = [s["kind"] for s in trace["steps"]]
    assert "reasoning" in kinds and "tool" in kinds
    # system snapshot + health event are gone
    assert trace["stats"]["events_dropped"] == 2
    assert all("SYSTEM" not in s["content"] for s in trace["steps"])
    # the long tool output was truncated and we saved tokens
    tool_step = next(s for s in trace["steps"] if s["kind"] == "tool")
    assert tool_step["truncated"] is True and tool_step["kept_tokens"] == 50
    assert trace["stats"]["savings_pct"] > 0


@pytest.mark.asyncio
async def test_trace_keep_tail_on_error(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid, task_id = await _make_task(ws)
    await _seed_trajectory(ws, task_id, tool_content="Traceback: boom " + "x " * 400)

    off = await auth_client.get(f"/api/quality/records/{tid}/trace?tool_output_token_cap=50")
    tool_off = next(s for s in off.json()["cleaned_trace"]["steps"] if s["kind"] == "tool")
    assert tool_off["truncated"] is True

    on = await auth_client.get(
        f"/api/quality/records/{tid}/trace?tool_output_token_cap=50&keep_tail_on_error=true"
    )
    tool_on = next(s for s in on.json()["cleaned_trace"]["steps"] if s["kind"] == "tool")
    assert tool_on["truncated"] is False


@pytest.mark.asyncio
async def test_trace_empty_task_ok(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid, _ = await _make_task(ws)
    r = await auth_client.get(f"/api/quality/records/{tid}/trace")
    assert r.status_code == 200, r.text
    assert r.json()["cleaned_trace"]["steps"] == []


@pytest.mark.asyncio
async def test_trace_rejects_out_of_range_cap(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid, _ = await _make_task(ws)
    r = await auth_client.get(f"/api/quality/records/{tid}/trace?tool_output_token_cap=5")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_trace_cross_workspace_404(auth_client: AsyncClient):
    tid, _ = await _make_task(DEFAULT_WORKSPACE_ID)
    r = await auth_client.get(f"/api/quality/records/{tid}/trace")
    assert r.status_code == 404
