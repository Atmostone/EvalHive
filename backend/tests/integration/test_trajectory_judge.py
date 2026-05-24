"""Integration tests for the Trajectory Judge endpoints (E-07).

POST /api/quality/records/{task_id}/evaluate-trajectory judges a task's real
trajectory (cleaned by E-06) with a mocked LLM and writes the profile to
quality_records.trajectory_profile; GET .../trajectory reads it back. Skipped
when there is no judge model or no trajectory steps; workspace-scoped.
"""

import json
import uuid
from datetime import datetime
from unittest.mock import MagicMock

from httpx import AsyncClient

from app import database
from app.models.agent_log import AgentLogChunk
from app.models.event import AgentEvent
from app.models.provider import LLMModel, Provider
from app.models.task import Task, TaskStatus
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace
from app.quality import trajectory as traj_mod

_AXIS_KEYS = [
    "efficiency",
    "tool_selection",
    "parameter_quality",
    "error_recovery",
    "goal_alignment",
    "loop_detection",
]


def _resp(pt=150, ct=40):
    args = {k: {"score": 7, "reason": "ok"} for k in _AXIS_KEYS}
    args["loop_detection"] = {"score": 9, "reason": "no loops"}
    args["summary"] = "decent path"
    fn = MagicMock()
    fn.arguments = json.dumps(args)
    tc = MagicMock()
    tc.function = fn
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(tool_calls=[tc]))]
    resp.usage = {"prompt_tokens": pt, "completion_tokens": ct}
    return resp


class _FakeProvider:
    async def acompletion(self, **kw):
        return _resp()


async def _seed_judge_model(s, workspace_id, *, kind="quality_judge_model_id"):
    prov = Provider(workspace_id=workspace_id, name="p", api_key="k", endpoint="http://x/v1")
    s.add(prov)
    await s.flush()
    model = LLMModel(
        provider_id=prov.id, display_name="M", api_name="m",
        input_price_per_1m_usd=1, output_price_per_1m_usd=2,
    )
    s.add(model)
    await s.flush()
    ws = await s.get(Workspace, workspace_id)
    setattr(ws, kind, model.id)
    return model


async def _seed_task_with_trace(ws, *, with_steps=True):
    async with database.async_session() as s:
        t = Task(title="build", status=TaskStatus.DONE.value, workspace_id=ws,
                 result_summary="r", model_used="m")
        s.add(t)
        await s.flush()
        if with_steps:
            s.add(AgentEvent(
                task_id=t.id, workspace_id=ws, event_type="orchestrator_reasoning",
                source="orchestrator", data={"reasoning": "pick the analytical template"},
                created_at=datetime(2026, 1, 1, 12, 0, 0),
            ))
            s.add(AgentLogChunk(
                task_id=t.id, workspace_id=ws, chunk_seq=0,
                content="search results line", tool_name="web_search",
            ))
        await s.commit()
        return str(t.id)


async def test_evaluate_trajectory_scored(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        await _seed_judge_model(s, ws)
        await s.commit()
    tid = await _seed_task_with_trace(ws)

    monkeypatch.setattr(traj_mod, "get_llm_provider", lambda: _FakeProvider())

    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-trajectory")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is False
    prof = body["trajectory_profile"]
    assert prof["status"] == "scored"
    assert len(prof["axes"]) == 6
    assert {a["key"] for a in prof["axes"]} == set(_AXIS_KEYS)
    assert prof["judge_model"] == "m"
    assert prof["overall_score"] is not None
    assert prof["loop_detected"] is False
    assert prof["judge_input_tokens"] == 150

    # readable back via GET trajectory
    r = await auth_client.get(f"/api/quality/records/{tid}/trajectory")
    assert r.status_code == 200
    assert r.json()["trajectory_profile"]["status"] == "scored"


async def test_evaluate_trajectory_skipped_without_model(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        wsrow = await s.get(Workspace, ws)
        wsrow.orchestrator_model_id = None
        wsrow.chat_model_id = None
        wsrow.memory_extractor_model_id = None
        wsrow.quality_judge_model_id = None
        await s.commit()
    tid = await _seed_task_with_trace(ws)

    monkeypatch.setattr(traj_mod, "get_llm_provider", lambda: _FakeProvider())
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-trajectory")
    assert r.status_code == 200
    assert r.json()["skipped"] is True
    assert r.json()["trajectory_profile"] is None


async def test_evaluate_trajectory_skipped_empty_trace(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        await _seed_judge_model(s, ws)
        await s.commit()
    tid = await _seed_task_with_trace(ws, with_steps=False)

    monkeypatch.setattr(traj_mod, "get_llm_provider", lambda: _FakeProvider())
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-trajectory")
    assert r.status_code == 200
    assert r.json()["skipped"] is True


async def test_evaluate_trajectory_cross_workspace_404(auth_client: AsyncClient):
    tid = await _seed_task_with_trace(DEFAULT_WORKSPACE_ID)
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-trajectory")
    assert r.status_code == 404
