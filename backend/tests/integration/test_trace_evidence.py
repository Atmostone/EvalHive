"""Integration tests for the TRACE Evidence Bank Judge endpoints (E-08).

POST /api/quality/records/{task_id}/evaluate-trajectory-evidence walks a task's
real trajectory (cleaned by E-06) step by step with a mocked LLM, accumulating an
evidence bank, and writes the profile to quality_records.trajectory_evidence_profile;
GET .../trajectory-evidence reads it back. Skipped when there is no judge model or
no trajectory steps; workspace-scoped.
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
from app.quality import trace_evidence as te_mod

_AXIS_KEYS = [
    "efficiency",
    "tool_selection",
    "parameter_quality",
    "error_recovery",
    "goal_alignment",
    "loop_detection",
]


def _resp(args, pt, ct):
    fn = MagicMock()
    fn.arguments = json.dumps(args)
    tc = MagicMock()
    tc.function = fn
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(tool_calls=[tc]))]
    resp.usage = {"prompt_tokens": pt, "completion_tokens": ct}
    return resp


class _FakeProvider:
    """Per-step assess_step responses + a final score_trajectory response."""

    async def acompletion(self, **kw):
        if kw["tools"][0]["function"]["name"] == "score_trajectory":
            args = {k: {"score": 7, "reason": "ok"} for k in _AXIS_KEYS}
            args["loop_detection"] = {"score": 9, "reason": "no loops"}
            args["summary"] = "grounded path"
            return _resp(args, 150, 40)
        args = {
            "redundant": False,
            "grounded": True,
            "progress": 8,
            "execution": 8,
            "new_facts": ["a fact"],
            "note": "ok step",
        }
        return _resp(args, 20, 8)


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


async def test_evaluate_evidence_scored(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        await _seed_judge_model(s, ws)
        await s.commit()
    tid = await _seed_task_with_trace(ws)

    monkeypatch.setattr(te_mod, "get_llm_provider", lambda: _FakeProvider())

    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-trajectory-evidence")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is False
    prof = body["trajectory_evidence_profile"]
    assert prof["status"] == "scored"
    assert len(prof["axes"]) == 6
    assert {a["key"] for a in prof["axes"]} == set(_AXIS_KEYS)
    assert prof["judge_model"] == "m"
    assert prof["overall_score"] is not None
    # 2 cleaned steps assessed → 2 per-step calls + 1 final
    assert prof["trace_stats"]["steps_assessed"] == 2
    assert prof["judge_calls"] == 3
    assert prof["groundedness"] == 1.0
    assert len(prof["evidence_bank"]) == 2
    assert prof["evidence_bank"][0]["facts"] == ["a fact"]
    # tokens summed over the N+1 calls
    assert prof["judge_input_tokens"] == 2 * 20 + 150

    # readable back via GET
    r = await auth_client.get(f"/api/quality/records/{tid}/trajectory-evidence")
    assert r.status_code == 200
    assert r.json()["trajectory_evidence_profile"]["status"] == "scored"


async def test_evaluate_evidence_skipped_without_model(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        wsrow = await s.get(Workspace, ws)
        wsrow.orchestrator_model_id = None
        wsrow.chat_model_id = None
        wsrow.memory_extractor_model_id = None
        wsrow.quality_judge_model_id = None
        await s.commit()
    tid = await _seed_task_with_trace(ws)

    monkeypatch.setattr(te_mod, "get_llm_provider", lambda: _FakeProvider())
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-trajectory-evidence")
    assert r.status_code == 200
    assert r.json()["skipped"] is True
    assert r.json()["trajectory_evidence_profile"] is None


async def test_evaluate_evidence_skipped_empty_trace(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        await _seed_judge_model(s, ws)
        await s.commit()
    tid = await _seed_task_with_trace(ws, with_steps=False)

    monkeypatch.setattr(te_mod, "get_llm_provider", lambda: _FakeProvider())
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-trajectory-evidence")
    assert r.status_code == 200
    assert r.json()["skipped"] is True


async def test_evaluate_evidence_cross_workspace_404(auth_client: AsyncClient):
    tid = await _seed_task_with_trace(DEFAULT_WORKSPACE_ID)
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-trajectory-evidence")
    assert r.status_code == 404
