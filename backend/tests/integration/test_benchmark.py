"""Historical Case Store records remain readable after SDK launch migration."""

import uuid

import pytest
from app import database
from app.models.quality_record import QualityRecord
from app.models.task import Task, TaskStatus
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality.benchmark import BenchmarkCase, materialize
from app.quality.data_lake import build_quality_record
from httpx import AsyncClient


def _case(cid="cap-1", **over):
    base = {
        "id": cid,
        "suite": "capability-isolation",
        "category": "fresh_data",
        "input": {"title": "weather today", "description": "look it up"},
        "gold": {
            "capability_spec": {"required_tools": ["web_search"]},
            "reference_answer": "sunny",
            "canonical_trajectory": ["web_search"],
        },
    }
    base.update(over)
    return BenchmarkCase(**base)


async def test_legacy_materialization_rejected_before_database_access():
    with pytest.raises(ValueError, match="Legacy benchmark execution"):
        await materialize(None, _case(), workspace_id=DEFAULT_WORKSPACE_ID, repeat=2)


async def test_build_quality_record_denormalizes_historical_benchmark(db_session):
    case = _case("cap-2")
    task = Task(
        title=case.input.title,
        status=TaskStatus.DONE.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
        benchmark_case_id=case.id,
        benchmark_suite=case.suite,
        reference_answer=case.gold.reference_answer,
        canonical_trajectory=case.gold.canonical_trajectory,
        capability_spec=case.gold.capability_spec,
        result_summary="sunny",
    )
    db_session.add(task)
    await db_session.commit()
    rec = await build_quality_record(db_session, task)
    assert rec is not None
    assert rec.benchmark_case_id == "cap-2"
    assert rec.benchmark_suite == "capability-isolation"
    assert task.reference_answer == "sunny"
    assert task.canonical_trajectory == ["web_search"]


def _scored(cls):
    return {"schema_version": 1, "status": "scored", "classification": cls,
            "category": "fresh_data"}


async def test_aggregate_suite_filter(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        # one record in suite A (genuine), one in suite B (cheated)
        for suite, cls in (("suite-a", "genuine"), ("suite-b", "cheated")):
            t = Task(title="t", status=TaskStatus.DONE.value, workspace_id=ws,
                     benchmark_suite=suite)
            s.add(t)
            await s.flush()
            s.add(QualityRecord(
                task_id=t.id, workspace_id=ws, model_used="m", final_status="done",
                benchmark_suite=suite, capability_profile=_scored(cls),
            ))
        await s.commit()

    # unscoped sees both; scoped sees only suite-a
    r = await auth_client.get("/api/quality/capability/aggregate")
    assert r.json()["total"] == 2
    r = await auth_client.get("/api/quality/capability/aggregate?suite=suite-a")
    body = r.json()
    assert body["filters"]["suite"] == "suite-a"
    assert body["total"] == 1 and body["genuine"] == 1
    assert body["capability_score"] == 1.0
