"""Integration tests for the Benchmark Case Store (pre-E-23).

materialize() turns a case into runnable READY task instances tagged with
benchmark_case_id/benchmark_suite; build_quality_record denormalizes those onto the
record; aggregate_capability (+ the API) can scope by suite.
"""

import uuid

from httpx import AsyncClient

from app import database
from app.models.quality_record import QualityRecord
from app.models.task import Task, TaskStatus
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality.benchmark import BenchmarkCase, materialize
from app.quality.data_lake import build_quality_record


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


async def test_materialize_links_and_gold(db_session):
    model_id = uuid.uuid4()
    tasks = await materialize(
        db_session, _case(), workspace_id=DEFAULT_WORKSPACE_ID,
        repeat=2, model_id=model_id,
    )
    assert len(tasks) == 2
    t = tasks[0]
    assert t.status == TaskStatus.READY.value
    assert t.benchmark_case_id == "cap-1"
    assert t.benchmark_suite == "capability-isolation"
    assert t.reference_answer == "sunny"
    assert t.canonical_trajectory == ["web_search"]
    # category carried into the capability_spec the E-13 harness reads
    assert t.capability_spec == {"required_tools": ["web_search"], "category": "fresh_data"}
    # model override threaded through run_config (no FK), no template pinned
    assert t.run_config == {"model_id": str(model_id)}
    assert t.template_id is None


async def test_build_quality_record_denormalizes_benchmark(db_session):
    tasks = await materialize(
        db_session, _case("cap-2"), workspace_id=DEFAULT_WORKSPACE_ID,
    )
    t = tasks[0]
    t.status = TaskStatus.DONE.value
    t.result_summary = "sunny"
    await db_session.commit()

    rec = await build_quality_record(db_session, t)
    assert rec is not None
    assert rec.benchmark_case_id == "cap-2"
    assert rec.benchmark_suite == "capability-isolation"


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
