"""Integration tests for the Reference-based Judge (E-03).

Task ``reference_answer`` round-trips through the API; rubric ``reference_mode``
persists; on-demand evaluation folds a reference dimension into the profile
(scored when a reference is set, skipped otherwise).
"""

import uuid

import pytest
from httpx import AsyncClient

from app import database
from app.models.provider import LLMModel, Provider
from app.models.task import Task, TaskStatus
from app.models.workspace import Workspace


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


@pytest.mark.asyncio
async def test_task_create_roundtrips_reference_answer(auth_client: AsyncClient):
    r = await auth_client.post("/api/tasks", json={
        "title": "Capital of France", "reference_answer": "Paris",
    })
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert r.json()["reference_answer"] == "Paris"

    r = await auth_client.get(f"/api/tasks/{tid}")
    assert r.json()["reference_answer"] == "Paris"


@pytest.mark.asyncio
async def test_rubric_reference_mode_persists(auth_client: AsyncClient):
    # reference dim keeps its mode
    body = {
        "name": "Ref Rubric", "dimensions": [
            {"key": "answer", "name": "Answer", "evaluator": "reference",
             "reference_mode": "semantic", "weight": 1.0, "threshold": 7, "critical": True},
        ],
    }
    r = await auth_client.post("/api/quality/rubrics", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["dimensions"][0]["reference_mode"] == "semantic"

    # non-reference evaluator has its reference_mode cleared
    body2 = {
        "name": "Judge Rubric", "dimensions": [
            {"key": "q", "name": "Q", "evaluator": "judge",
             "reference_mode": "fuzzy", "weight": 1.0, "threshold": 5, "critical": False},
        ],
    }
    r = await auth_client.post("/api/quality/rubrics", json=body2)
    assert r.status_code == 200, r.text
    assert r.json()["dimensions"][0]["reference_mode"] is None

    # reference dim without an explicit mode defaults to pointwise
    body3 = {
        "name": "Default Mode Rubric", "dimensions": [
            {"key": "answer", "name": "Answer", "evaluator": "reference",
             "weight": 1.0, "threshold": 5, "critical": False},
        ],
    }
    r = await auth_client.post("/api/quality/rubrics", json=body3)
    assert r.json()["dimensions"][0]["reference_mode"] == "pointwise"


@pytest.mark.asyncio
async def test_evaluate_reference_dimension_scored(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])

    # workspace-default rubric with a single exact-match reference dimension
    r = await auth_client.post("/api/quality/rubrics", json={
        "name": "Exact", "is_default": True, "dimensions": [
            {"key": "answer", "name": "Answer", "evaluator": "reference",
             "reference_mode": "exact", "weight": 1.0, "threshold": 6, "critical": True},
        ],
    })
    assert r.status_code == 200, r.text

    async with database.async_session() as s:
        await _seed_judge_model(s, ws)
        t = Task(title="capital", status=TaskStatus.DONE.value, workspace_id=ws,
                 result_summary="Paris", reference_answer="paris", model_used="m")
        s.add(t)
        await s.commit()
        tid = str(t.id)

    # exact mode is pure-python: no LLM needed even though a judge model exists
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate")
    assert r.status_code == 200, r.text
    profile = r.json()["quality_profile"]
    dim = profile["dimensions"][0]
    assert dim["evaluator"] == "reference" and dim["reference_mode"] == "exact"
    assert dim["status"] == "scored" and dim["score"] == 10 and dim["passed"] is True
    assert profile["gate"]["passed"] is True
    assert profile["schema_version"] == 2


@pytest.mark.asyncio
async def test_evaluate_reference_dimension_skipped(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])

    r = await auth_client.post("/api/quality/rubrics", json={
        "name": "Exact2", "is_default": True, "dimensions": [
            {"key": "answer", "name": "Answer", "evaluator": "reference",
             "reference_mode": "exact", "weight": 1.0, "threshold": 6, "critical": True},
        ],
    })
    assert r.status_code == 200, r.text

    async with database.async_session() as s:
        await _seed_judge_model(s, ws)
        t = Task(title="capital", status=TaskStatus.DONE.value, workspace_id=ws,
                 result_summary="Paris", reference_answer=None, model_used="m")
        s.add(t)
        await s.commit()
        tid = str(t.id)

    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate")
    assert r.status_code == 200, r.text
    profile = r.json()["quality_profile"]
    dim = profile["dimensions"][0]
    # no reference_answer → skipped; critical dim does not fail the gate
    assert dim["status"] == "skipped" and dim["score"] is None
    assert profile["gate"]["passed"] is True
    assert profile["weighted_score"] is None
