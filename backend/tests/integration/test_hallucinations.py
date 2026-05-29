"""Integration tests for the Hallucination Detection endpoints (E-15).

POST /api/quality/records/{task_id}/evaluate-hallucinations fact-checks a task's
real deliverable (``result_summary``) against its cleaned trajectory (E-06): URLs
are checked deterministically (a URL is supported iff it appears in the trace),
while numbers/claims/uncertain APIs go to a single mocked LLM call. The per-
category profile is written to quality_records.hallucination_profile; GET
.../hallucinations reads it back; GET /api/quality/hallucinations/aggregate
returns the per-(model, category) distribution. Skipped when there is no judge
model, no deliverable, or no trajectory; workspace-scoped.
"""

import json
import uuid
from unittest.mock import MagicMock

from httpx import AsyncClient

from app import database
from app.models.agent_log import AgentLogChunk
from app.models.provider import LLMModel, Provider
from app.models.task import Task, TaskStatus
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace
from app.quality import hallucination as hl_mod

_REAL_URL = "https://real.org/report"
_FAKE_URL = "https://invented.example/never-fetched"
_RESULT = (
    f"We fetched figures from {_REAL_URL} which confirm the trend. "
    f"We also cite {_FAKE_URL} that was never actually visited. "
    "Revenue grew 42% over the prior fiscal period according to the analysis."
)


def _resp(pt=150, ct=40):
    args = {
        "apis": [],
        "numbers": [
            {"value": "42%", "supported": False,
             "reason": "no source in trace for this figure", "confidence": 0.8}
        ],
        "citations": [],
        "summary": "Cites an unvisited URL and an unsourced figure.",
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


async def _seed_task_with_trace(ws, *, with_steps=True, result=_RESULT):
    async with database.async_session() as s:
        t = Task(title="build", status=TaskStatus.DONE.value, workspace_id=ws,
                 result_summary=result, model_used="m")
        s.add(t)
        await s.flush()
        if with_steps:
            s.add(AgentLogChunk(
                task_id=t.id, workspace_id=ws, chunk_seq=0,
                content=f"visited {_REAL_URL} returning the quarterly figures",
                tool_name="web_fetch",
            ))
        await s.commit()
        return str(t.id)


async def test_evaluate_hallucinations_scored(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        await _seed_judge_model(s, ws)
        await s.commit()
    tid = await _seed_task_with_trace(ws)

    monkeypatch.setattr(hl_mod, "get_llm_provider", lambda: _FakeProvider())

    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-hallucinations")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is False
    prof = body["hallucination_profile"]
    assert prof["status"] == "scored"
    # One URL fetched (supported), one invented (hallucinated).
    assert prof["categories"]["urls"]["checked"] == 2
    assert prof["categories"]["urls"]["hallucinated"] == 1
    assert prof["categories"]["urls"]["items"][0]["value"] == _FAKE_URL
    assert prof["categories"]["urls"]["items"][0]["kind"] == "deterministic"
    # The unsourced figure is flagged by the (mocked) LLM.
    assert prof["categories"]["numbers"]["hallucinated"] == 1
    assert prof["categories"]["numbers"]["items"][0]["value"] == "42%"
    assert prof["categories"]["numbers"]["items"][0]["kind"] == "llm"
    assert prof["hallucination_count"] >= 2
    assert prof["judge_model"] == "m"
    assert prof["judge_input_tokens"] == 150

    # readable back via GET hallucinations
    r = await auth_client.get(f"/api/quality/records/{tid}/hallucinations")
    assert r.status_code == 200
    assert r.json()["hallucination_profile"]["status"] == "scored"

    # aggregate shows the per-category distribution by model
    r = await auth_client.get("/api/quality/hallucinations/aggregate")
    assert r.status_code == 200
    agg = r.json()
    assert agg["runs_total"] >= 1
    assert agg["by_category"]["urls"]["by_category"]["urls"]["hallucinated"] >= 1
    assert "m" in agg["by_model"]


async def test_evaluate_hallucinations_skipped_without_model(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        wsrow = await s.get(Workspace, ws)
        wsrow.orchestrator_model_id = None
        wsrow.chat_model_id = None
        wsrow.memory_extractor_model_id = None
        wsrow.quality_judge_model_id = None
        await s.commit()
    tid = await _seed_task_with_trace(ws)

    monkeypatch.setattr(hl_mod, "get_llm_provider", lambda: _FakeProvider())
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-hallucinations")
    assert r.status_code == 200
    assert r.json()["skipped"] is True
    assert r.json()["hallucination_profile"] is None


async def test_evaluate_hallucinations_skipped_no_deliverable(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        await _seed_judge_model(s, ws)
        await s.commit()
    tid = await _seed_task_with_trace(ws, result="")

    monkeypatch.setattr(hl_mod, "get_llm_provider", lambda: _FakeProvider())
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-hallucinations")
    assert r.status_code == 200
    assert r.json()["skipped"] is True


async def test_evaluate_hallucinations_cross_workspace_404(auth_client: AsyncClient):
    tid = await _seed_task_with_trace(DEFAULT_WORKSPACE_ID)
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-hallucinations")
    assert r.status_code == 404
