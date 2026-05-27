"""Integration tests for the Failure Mode Classifier endpoints (E-14).

POST /api/quality/records/{task_id}/evaluate-failure-modes classifies a task's
real trajectory (cleaned by E-06) with a mocked LLM and writes the multi-label
profile to quality_records.failure_profile; GET .../failure-modes reads it back;
GET /api/quality/failure-modes/aggregate returns the per-class distribution.
Skipped when there is no judge model or no trajectory steps; workspace-scoped.
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
from app.quality import failure_modes as fm_mod


def _resp(pt=150, ct=40):
    args = {
        "failures": [
            {"class": "loop", "confidence": 0.8, "reason": "repeated search"},
            {"class": "ignored_error", "confidence": 0.5, "reason": "ignored 404"},
        ],
        "summary": "looped and ignored an error",
    }
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


async def test_evaluate_failure_modes_scored(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        await _seed_judge_model(s, ws)
        await s.commit()
    tid = await _seed_task_with_trace(ws)

    monkeypatch.setattr(fm_mod, "get_llm_provider", lambda: _FakeProvider())

    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-failure-modes")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is False
    prof = body["failure_profile"]
    assert prof["status"] == "scored"
    assert [f["class"] for f in prof["failures"]] == ["loop", "ignored_error"]
    assert prof["judge_model"] == "m"
    assert prof["judge_input_tokens"] == 150

    # readable back via GET failure-modes
    r = await auth_client.get(f"/api/quality/records/{tid}/failure-modes")
    assert r.status_code == 200
    assert r.json()["failure_profile"]["status"] == "scored"

    # aggregate shows the per-class distribution by model
    r = await auth_client.get("/api/quality/failure-modes/aggregate")
    assert r.status_code == 200
    agg = r.json()
    assert agg["runs_total"] >= 1
    assert agg["by_class"]["loop"]["runs_total"] >= 1
    assert "m" in agg["by_model"]


async def test_evaluate_failure_modes_skipped_without_model(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        wsrow = await s.get(Workspace, ws)
        wsrow.orchestrator_model_id = None
        wsrow.chat_model_id = None
        wsrow.memory_extractor_model_id = None
        wsrow.quality_judge_model_id = None
        await s.commit()
    tid = await _seed_task_with_trace(ws)

    monkeypatch.setattr(fm_mod, "get_llm_provider", lambda: _FakeProvider())
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-failure-modes")
    assert r.status_code == 200
    assert r.json()["skipped"] is True
    assert r.json()["failure_profile"] is None


async def test_evaluate_failure_modes_skipped_empty_trace(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        await _seed_judge_model(s, ws)
        await s.commit()
    tid = await _seed_task_with_trace(ws, with_steps=False)

    monkeypatch.setattr(fm_mod, "get_llm_provider", lambda: _FakeProvider())
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-failure-modes")
    assert r.status_code == 200
    assert r.json()["skipped"] is True


async def test_evaluate_failure_modes_cross_workspace_404(auth_client: AsyncClient):
    tid = await _seed_task_with_trace(DEFAULT_WORKSPACE_ID)
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-failure-modes")
    assert r.status_code == 404
