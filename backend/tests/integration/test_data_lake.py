"""Integration tests for the Quality Data Lake (E-01): build, API, jobs."""

import io
import json
import uuid
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app import database
from app.models.event import AgentEvent
from app.models.quality_record import QualityRecord
from app.models.scheduled_job import ScheduledJob
from app.models.setting import Setting
from app.models.task import Task, TaskStatus
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality.data_lake import build_quality_record
from app.storage.minio_client import read_quality_record


async def _add_spawn_event(s, task, *, template_name="Coder", soul="s", model="m"):
    s.add(AgentEvent(
        task_id=task.id, event_type="agent_spawned", source="orchestrator",
        data={"template_name": template_name, "soul_md": soul, "model_api_name": model},
        workspace_id=task.workspace_id,
    ))


@pytest.mark.asyncio
async def test_build_quality_record_idempotent_and_blob(db_session):
    task = Task(
        title="x", status=TaskStatus.AWAITING_APPROVAL.value,
        workspace_id=DEFAULT_WORKSPACE_ID, model_used="m",
        token_usage={"input_tokens": 10, "output_tokens": 5},
    )
    db_session.add(task)
    await db_session.flush()
    await _add_spawn_event(db_session, task)
    await db_session.flush()

    rec1 = await build_quality_record(db_session, task)
    assert rec1.record_s3_path
    assert rec1.public_dataset_opt_in is False  # privacy default
    assert rec1.template_name == "Coder"
    assert rec1.final_status == TaskStatus.AWAITING_APPROVAL.value

    # rebuild after the verdict → same row, reconciled final_status, no duplicate
    task.status = TaskStatus.DONE.value
    rec2 = await build_quality_record(db_session, task)
    assert rec2.id == rec1.id
    assert rec2.final_status == TaskStatus.DONE.value

    count = (
        await db_session.execute(
            select(func.count()).select_from(QualityRecord).where(
                QualityRecord.task_id == task.id
            )
        )
    ).scalar()
    assert count == 1

    blob = json.loads(read_quality_record(rec1.record_s3_path))
    assert blob["execution"]["soul_md"] == "s"
    assert blob["task"]["input_tokens"] == 10


@pytest.mark.asyncio
async def test_records_query_export(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        for i, (m, st) in enumerate([("gpt-a", "done"), ("gpt-b", "failed")]):
            t = Task(title=f"Python task {i}", status=st, workspace_id=ws, model_used=m)
            s.add(t)
            await s.flush()
            s.add(QualityRecord(
                task_id=t.id, workspace_id=ws, template_name="Coder", model_used=m,
                final_status=st, cost_usd=1, input_tokens=10, output_tokens=5,
                duration_seconds=3, tool_call_count=2,
            ))
        await s.commit()

    # list
    r = await auth_client.get("/api/data-lake/records")
    assert r.status_code == 200
    assert len(r.json()) == 2

    # filter
    r = await auth_client.get("/api/data-lake/records", params={"model_used": "gpt-a"})
    assert len(r.json()) == 1

    # group-by query
    r = await auth_client.get("/api/data-lake/query", params={"group_by": "model_used"})
    groups = {g["group"]: g for g in r.json()}
    assert set(groups) == {"gpt-a", "gpt-b"}
    assert groups["gpt-a"]["approval_rate"] == 1.0
    assert groups["gpt-b"]["approval_rate"] == 0.0

    # title_contains lens
    r = await auth_client.get("/api/data-lake/query", params={"group_by": "template_name", "title_contains": "Python"})
    assert r.json()[0]["count"] == 2

    # JSON export
    r = await auth_client.get("/api/data-lake/export", params={"format": "json"})
    assert r.status_code == 200
    assert len(json.loads(r.content)) == 2

    # Parquet export round-trips
    r = await auth_client.get("/api/data-lake/export", params={"format": "parquet"})
    assert r.status_code == 200
    import pyarrow.parquet as pq
    table = pq.read_table(io.BytesIO(r.content))
    assert table.num_rows == 2
    assert "model_used" in table.column_names


@pytest.mark.asyncio
async def test_get_record_returns_blob(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        t = Task(title="x", status=TaskStatus.DONE.value, workspace_id=ws, model_used="m")
        s.add(t)
        await s.flush()
        await _add_spawn_event(s, t, soul="hello")
        await s.commit()
        await build_quality_record(s, t)
        tid = str(t.id)

    r = await auth_client.get(f"/api/data-lake/records/{tid}")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["model_used"] == "m"
    assert body["record"]["execution"]["soul_md"] == "hello"


@pytest.mark.asyncio
async def test_workspace_isolation(auth_client: AsyncClient):
    # record in the DEFAULT workspace must be invisible to the auth client's workspace
    async with database.async_session() as s:
        t = Task(title="x", status=TaskStatus.DONE.value, workspace_id=DEFAULT_WORKSPACE_ID, model_used="m")
        s.add(t)
        await s.flush()
        s.add(QualityRecord(task_id=t.id, workspace_id=DEFAULT_WORKSPACE_ID, final_status="done"))
        await s.commit()
        tid = str(t.id)

    r = await auth_client.get(f"/api/data-lake/records/{tid}")
    assert r.status_code == 404
    r = await auth_client.get("/api/data-lake/records")
    assert r.json() == []


@pytest.mark.asyncio
async def test_backfill_builds_missing(db_session):
    from app.scheduler import _job_runner

    async with database.async_session() as s:
        t = Task(title="x", status=TaskStatus.DONE.value, workspace_id=DEFAULT_WORKSPACE_ID, model_used="m")
        s.add(t)
        await s.flush()
        await _add_spawn_event(s, t)
        job = ScheduledJob(
            name="qb-test", kind="interval", interval_seconds=300,
            payload={"action": "quality_record_backfill"},
            workspace_id=DEFAULT_WORKSPACE_ID, enabled=True,
        )
        s.add(job)
        await s.commit()
        jid, tid = str(job.id), t.id

    await _job_runner(jid)

    async with database.async_session() as s:
        rec = (
            await s.execute(select(QualityRecord).where(QualityRecord.task_id == tid))
        ).scalar_one_or_none()
        assert rec is not None
        assert rec.final_status == TaskStatus.DONE.value
        assert rec.record_s3_path


@pytest.mark.asyncio
async def test_retention_prunes_old(db_session):
    from app.scheduler import _job_runner

    async with database.async_session() as s:
        existing = await s.get(Setting, "data_lake_retention_days")
        if existing:
            existing.value = 7
        else:
            s.add(Setting(key="data_lake_retention_days", value=7))
        t = Task(title="x", status=TaskStatus.DONE.value, workspace_id=DEFAULT_WORKSPACE_ID, model_used="m")
        s.add(t)
        await s.flush()
        s.add(QualityRecord(
            task_id=t.id, workspace_id=DEFAULT_WORKSPACE_ID, final_status="done",
            created_at=datetime.utcnow() - timedelta(days=30), public_dataset_opt_in=False,
        ))
        job = ScheduledJob(
            name="qr-test", kind="cron", cron_expr="30 0 * * *",
            payload={"action": "quality_record_retention"},
            workspace_id=DEFAULT_WORKSPACE_ID, enabled=True,
        )
        s.add(job)
        await s.commit()
        jid, tid = str(job.id), t.id

    await _job_runner(jid)

    async with database.async_session() as s:
        rec = (
            await s.execute(select(QualityRecord).where(QualityRecord.task_id == tid))
        ).scalar_one_or_none()
        assert rec is None


@pytest.mark.asyncio
async def test_tool_call_count_counts_calls_not_log_rows(db_session):
    """A row is a transport artefact — the agent writes the call before running the
    tool, the result after, and a large output splits further. Counting rows made a
    single call count two or more times, and `tool_call_count` feeds `steps_mean`
    in the experiment report (SPA-86 review)."""
    from app.models.agent_log import AgentLogChunk

    task = Task(
        title="calls", status=TaskStatus.DONE.value,
        workspace_id=DEFAULT_WORKSPACE_ID, model_used="m",
    )
    db_session.add(task)
    await db_session.flush()
    await _add_spawn_event(db_session, task)

    # call A: call record + a single-part result. call B: call record + 3 parts.
    rows = [
        ("file_write", "cA", 0, 1, {"path": "a.md"}),
        ("file_write", "cA", 1, 2, {"path": "a.md"}),
        ("bash", "cB", 0, 1, {"command": "cat big"}),
        ("bash", "cB", 1, 4, {"command": "cat big"}),
        ("bash", "cB", 2, 4, {"command": "cat big"}),
        ("bash", "cB", 3, 4, {"command": "cat big"}),
    ]
    for seq, (tool, call_id, part_index, part_total, args) in enumerate(rows):
        db_session.add(AgentLogChunk(
            task_id=task.id, workspace_id=task.workspace_id, chunk_seq=seq,
            content=f"chunk-{seq}", tool_name=tool, tool_call_id=call_id,
            part_index=part_index, part_total=part_total, arguments=args,
        ))
    await db_session.commit()

    record = await build_quality_record(db_session, task)
    assert record is not None
    assert record.tool_call_count == 2, "6 log rows are 2 tool calls"

    blob = read_quality_record(record.record_s3_path)
    calls = json.loads(blob.decode())["execution"]["tool_calls"]
    assert [c["tool_name"] for c in calls] == ["file_write", "bash"]
    # The parameters are frozen with the call — `parameter_quality` cannot be
    # re-examined later from a blob that only knows the tool's name.
    assert calls[0]["arguments"] == {"path": "a.md"}
    assert calls[1]["arguments"] == {"command": "cat big"}
