"""Integration tests for Human Feedback Collection (E-05).

Feedback round-trips through PUT/GET, auto-creates the quality record when none
exists, pairs human scores with judge scores from the profile, surfaces in the
calibration export, is workspace-scoped, and rejects out-of-range scores.
"""

import uuid

import pytest
from httpx import AsyncClient

from app import database
from app.models.provider import LLMModel, Provider
from app.models.task import Task, TaskStatus
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace


async def _seed_judge_model(s, workspace_id):
    prov = Provider(workspace_id=workspace_id, name="p", api_key="k", endpoint="http://x/v1")
    s.add(prov)
    await s.flush()
    model = LLMModel(provider_id=prov.id, display_name="M", api_name="m",
                     input_price_per_1m_usd=1, output_price_per_1m_usd=2)
    s.add(model)
    await s.flush()
    ws = await s.get(Workspace, workspace_id)
    ws.quality_judge_model_id = model.id


async def _make_task(ws, **kw):
    kw.setdefault("result_summary", "result")
    async with database.async_session() as s:
        t = Task(title="t", status=TaskStatus.DONE.value, workspace_id=ws,
                 model_used="m", **kw)
        s.add(t)
        await s.commit()
        return str(t.id)


@pytest.mark.asyncio
async def test_feedback_roundtrip_and_record_autocreate(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _make_task(ws)

    # no feedback yet
    r = await auth_client.get(f"/api/quality/records/{tid}/feedback")
    assert r.status_code == 200, r.text
    assert r.json()["human_feedback"] is None

    # submit — record is built on demand
    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "verdict": "approve",
        "overall_comment": "solid",
        "dimensions": [{"key": "correctness", "name": "Correctness", "score": 9, "comment": "right"}],
    })
    assert r.status_code == 200, r.text
    fb = r.json()["human_feedback"]
    assert fb["verdict"] == "approve" and fb["overall_comment"] == "solid"
    d = fb["dimensions"][0]
    assert d["score"] == 9 and d["band"] == "good" and d["comment"] == "right"
    assert d["judge_score"] is None  # no profile yet
    assert fb["submitted_by"]  # the authed user's email

    # GET returns the stored feedback
    r = await auth_client.get(f"/api/quality/records/{tid}/feedback")
    assert r.json()["human_feedback"]["dimensions"][0]["score"] == 9


@pytest.mark.asyncio
async def test_feedback_pairs_judge_score_and_exports(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])

    # default rubric with a pure-python reference dim → profile without an LLM
    r = await auth_client.post("/api/quality/rubrics", json={
        "name": "Exact", "is_default": True, "dimensions": [
            {"key": "answer", "name": "Answer", "evaluator": "reference",
             "reference_mode": "exact", "weight": 1.0, "threshold": 6, "critical": True},
        ],
    })
    assert r.status_code == 200, r.text

    async with database.async_session() as s:
        await _seed_judge_model(s, ws)
    tid = await _make_task(ws, result_summary="Paris", reference_answer="paris")

    # evaluate → profile with answer=10
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate")
    assert r.status_code == 200, r.text
    assert r.json()["quality_profile"]["dimensions"][0]["score"] == 10

    # human disagrees: rates the same dimension 4
    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "dimensions": [{"key": "answer", "name": "Answer", "score": 4}],
    })
    assert r.status_code == 200, r.text
    d = r.json()["human_feedback"]["dimensions"][0]
    assert d["score"] == 4 and d["band"] == "improve" and d["judge_score"] == 10

    # calibration export pairs them
    r = await auth_client.get("/api/quality/calibration")
    assert r.status_code == 200, r.text
    rows = [row for row in r.json() if row["task_id"] == tid]
    assert len(rows) == 1
    assert rows[0]["judge_score"] == 10 and rows[0]["human_score"] == 4
    assert rows[0]["dimension_key"] == "answer"


@pytest.mark.asyncio
async def test_feedback_rejects_out_of_range_score(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _make_task(ws)
    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "dimensions": [{"key": "a", "name": "A", "score": 11}],
    })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_feedback_cross_workspace_404(auth_client: AsyncClient):
    # a task in another workspace (the seeded default) is invisible to this client
    tid = await _make_task(DEFAULT_WORKSPACE_ID)

    r = await auth_client.get(f"/api/quality/records/{tid}/feedback")
    assert r.status_code == 404
    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={"dimensions": []})
    assert r.status_code == 404
