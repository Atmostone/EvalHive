"""Integration tests for the Behavioral / objective evaluator (E-04).

Rubric ``probe`` persists/clears through the API; on-demand evaluation folds an
objective dimension into the profile (scored when code artifacts exist, skipped
otherwise). Artifact storage is monkeypatched so the test needs no MinIO.
"""

import uuid

import pytest
from httpx import AsyncClient

from app import database
from app.models.provider import LLMModel, Provider
from app.models.task import Task, TaskStatus
from app.models.workspace import Workspace
from app.quality import objective as obj

CLEAN = b"x = 1\n"


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
async def test_rubric_probe_persists(auth_client: AsyncClient):
    # objective dim keeps its probe
    r = await auth_client.post("/api/quality/rubrics", json={
        "name": "Obj Rubric", "dimensions": [
            {"key": "code", "name": "Code", "evaluator": "objective",
             "probe": "types", "weight": 1.0, "threshold": 7, "critical": True},
        ],
    })
    assert r.status_code == 200, r.text
    assert r.json()["dimensions"][0]["probe"] == "types"

    # non-objective evaluator has its probe cleared
    r = await auth_client.post("/api/quality/rubrics", json={
        "name": "Judge Rubric", "dimensions": [
            {"key": "q", "name": "Q", "evaluator": "judge",
             "probe": "lint", "weight": 1.0, "threshold": 5, "critical": False},
        ],
    })
    assert r.status_code == 200, r.text
    assert r.json()["dimensions"][0]["probe"] is None

    # objective dim without an explicit probe defaults to lint
    r = await auth_client.post("/api/quality/rubrics", json={
        "name": "Default Probe Rubric", "dimensions": [
            {"key": "code", "name": "Code", "evaluator": "objective",
             "weight": 1.0, "threshold": 5, "critical": False},
        ],
    })
    assert r.json()["dimensions"][0]["probe"] == "lint"


@pytest.mark.asyncio
async def test_evaluate_objective_dimension_scored(auth_client: AsyncClient, monkeypatch):
    obj._CACHE.clear()
    monkeypatch.setattr(obj, "_read_artifact", lambda path: CLEAN)
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])

    r = await auth_client.post("/api/quality/rubrics", json={
        "name": "Lint", "is_default": True, "dimensions": [
            {"key": "code", "name": "Code", "evaluator": "objective",
             "probe": "lint", "weight": 1.0, "threshold": 6, "critical": True},
        ],
    })
    assert r.status_code == 200, r.text

    async with database.async_session() as s:
        await _seed_judge_model(s, ws)
        t = Task(title="impl", status=TaskStatus.DONE.value, workspace_id=ws,
                 result_summary="done", result_files=["results/t/clean.py"], model_used="m")
        s.add(t)
        await s.commit()
        tid = str(t.id)

    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate")
    assert r.status_code == 200, r.text
    profile = r.json()["quality_profile"]
    dim = profile["dimensions"][0]
    assert dim["evaluator"] == "objective" and dim["probe"] == "lint"
    assert dim["status"] == "scored" and dim["score"] == 10 and dim["passed"] is True
    assert profile["gate"]["passed"] is True
    assert profile["schema_version"] == 2


@pytest.mark.asyncio
async def test_evaluate_objective_dimension_skipped(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])

    r = await auth_client.post("/api/quality/rubrics", json={
        "name": "Lint2", "is_default": True, "dimensions": [
            {"key": "code", "name": "Code", "evaluator": "objective",
             "probe": "lint", "weight": 1.0, "threshold": 6, "critical": True},
        ],
    })
    assert r.status_code == 200, r.text

    async with database.async_session() as s:
        await _seed_judge_model(s, ws)
        t = Task(title="report", status=TaskStatus.DONE.value, workspace_id=ws,
                 result_summary="done", result_files=["results/t/report.md"], model_used="m")
        s.add(t)
        await s.commit()
        tid = str(t.id)

    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate")
    assert r.status_code == 200, r.text
    profile = r.json()["quality_profile"]
    dim = profile["dimensions"][0]
    # no Python artifact → skipped; critical dim does not fail the gate
    assert dim["status"] == "skipped" and dim["score"] is None
    assert profile["gate"]["passed"] is True
    assert profile["weighted_score"] is None
