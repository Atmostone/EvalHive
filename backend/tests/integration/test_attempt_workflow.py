"""Review, retries and archival must preserve the attempt's measurement source."""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from app.auth.tokens import _utcnow_naive
from app.harnesses.attempts import create_attempt, ingest_events
from app.harnesses.catalog import get_manifest
from app.harnesses.executor import settle_task
from app.models.event import AgentEvent
from app.models.quality_record import QualityRecord
from app.models.task import Task
from sqlalchemy import select


@pytest.fixture
def finished(auth_client, db_session):
    async def make(*, mode="direct", outcome="success", benchmark=False, retries=1):
        workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
        task = Task(title="Workflow fixture", status="in_progress", workspace_id=workspace,
                    max_retries=retries, retry_count=0, run_config={"benchmark_mode": benchmark})
        db_session.add(task)
        await db_session.flush()
        spec = {
            "protocol_version": "1.0", "workspace_id": str(workspace), "task_id": str(task.id),
            "attempt_id": str(uuid.uuid4()), "task": {"prompt": "Offline workflow fixture"},
            "cwd": "/workspace", "config": {}, "metadata": {"execution_mode": mode},
            "environment": {"id": "workspace", "resources": [{"id": "memory", "kind": "memory", "config": {"mode": "off"}}]},
        }
        attempt = await create_attempt(db_session, workspace_id=workspace, task_id=task.id,
                                       manifest=get_manifest("pi"), run_spec=spec)
        result = {"status": outcome, "summary": "Native result"}
        if outcome == "failed":
            result["error"] = {"code": "MODEL_ERROR", "message": "Offline failure fixture"}
        frames = [("run.started", {}), ("run.finished", result)]
        await ingest_events(db_session, attempt.id, [
            {"protocol_version": "1.0", "type": "event", "attempt_id": str(attempt.id),
             "event_id": str(seq), "seq": seq, "source": "test", "event": name, "data": data}
            for seq, (name, data) in enumerate(frames)
        ])
        await db_session.commit()
        return task, attempt

    return make


@pytest.fixture
def review(monkeypatch):
    resolve = AsyncMock(return_value=object())
    evaluate = AsyncMock(return_value={"approved": True, "feedback": ""})
    memory = AsyncMock()
    monkeypatch.setattr("app.api._resolve_model.resolve_workspace_model", resolve)
    monkeypatch.setattr("app.orchestrator.llm.evaluate_agent_result", evaluate)
    monkeypatch.setattr("app.memory.extractor.extract_memory", memory)
    return evaluate, resolve, memory


@pytest.mark.parametrize("mode,benchmark", [("direct", False), ("orchestrated", True)])
@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled"])
async def test_direct_and_benchmark_archive_without_auxiliary_models(db_session, finished, review, mode, benchmark, outcome):
    task, attempt = await finished(mode=mode, benchmark=benchmark, outcome=outcome)
    await settle_task(db_session, attempt.id)
    await settle_task(db_session, attempt.id)
    record = await db_session.scalar(select(QualityRecord).where(QualityRecord.task_id == task.id))
    assert task.status == ("done" if outcome == "success" else "failed")
    assert record is not None and record.execution_attempt_id == attempt.id
    assert attempt.completeness["workflow"] == "archived"
    assert task.retry_count == 0 and task.cost_usd is None
    for mock in review:
        mock.assert_not_called()


@pytest.mark.parametrize("approved,expected,retries", [(True, "awaiting_approval", 1), (False, "ready", 1), (False, "failed", 0), (None, "awaiting_approval", 1)])
async def test_review_decision_and_retry_are_idempotent(db_session, finished, review, approved, expected, retries):
    task, attempt = await finished(mode="orchestrated", retries=retries)
    review[0].return_value = {"approved": approved, "feedback": "Please correct the output"}
    await settle_task(db_session, attempt.id)
    await settle_task(db_session, attempt.id)
    assert task.status == expected
    review[0].assert_awaited_once()
    review[2].assert_not_called()
    record = await db_session.scalar(select(QualityRecord).where(QualityRecord.task_id == task.id))
    assert (record is None) == (expected == "ready")
    assert attempt.status == "success"  # Review never rewrites native evidence.
    if expected == "ready":
        assert task.retry_count == 1 and task.orchestrator_feedback == "Please correct the output"


async def test_unavailable_review_requires_owner_without_fabricated_approval(db_session, finished, review):
    task, attempt = await finished(mode="orchestrated")
    review[0].side_effect = RuntimeError("Private diagnostic must not enter task events")
    await settle_task(db_session, attempt.id)
    assert task.status == "awaiting_approval" and attempt.completeness["review"] == "unavailable"
    events = (await db_session.scalars(select(AgentEvent).where(AgentEvent.task_id == task.id))).all()
    assert "Private diagnostic" not in str([event.data for event in events])
    assert any(event.data.get("action") == "review_requires_owner" for event in events)


async def test_interrupted_review_is_not_repeated(db_session, finished, review):
    task, attempt = await finished(mode="orchestrated")
    task.status = "review"
    attempt.completeness = {**attempt.completeness, "task_projection": "committed", "workflow": "review_running",
                            "review_started_at": (_utcnow_naive() - timedelta(minutes=6)).isoformat()}
    await db_session.commit()
    await settle_task(db_session, attempt.id)
    assert task.status == "awaiting_approval"
    assert attempt.completeness["review"] == "unknown_after_interruption"
    for mock in review:
        mock.assert_not_called()


async def test_review_claim_is_left_alone_during_lease(db_session, finished, review):
    task, attempt = await finished(mode="orchestrated")
    task.status = "review"
    attempt.completeness = {**attempt.completeness, "task_projection": "committed", "workflow": "review_running",
                            "review_started_at": _utcnow_naive().isoformat()}
    await db_session.commit()
    await settle_task(db_session, attempt.id)
    assert task.status == "review" and attempt.completeness["workflow"] == "review_running"
    for mock in review:
        mock.assert_not_called()


async def test_archive_failure_resumes_without_repeating_review(db_session, finished, review, monkeypatch):
    from app.quality import data_lake

    task, attempt = await finished(mode="orchestrated")
    original = data_lake.build_quality_record
    monkeypatch.setattr(data_lake, "build_quality_record", AsyncMock(side_effect=OSError("storage down")))
    await settle_task(db_session, attempt.id)
    assert attempt.completeness["workflow"] == "archive_pending" and task.status == "awaiting_approval"
    monkeypatch.setattr(data_lake, "build_quality_record", original)
    await settle_task(db_session, attempt.id)
    assert attempt.completeness["workflow"] == "archived"
    review[0].assert_awaited_once()


@pytest.mark.parametrize("outcome,expected", [("failed", "ready"), ("cancelled", "failed")])
async def test_failed_execution_can_retry_but_cancellation_cannot(db_session, finished, review, outcome, expected):
    task, attempt = await finished(mode="orchestrated", outcome=outcome)
    await settle_task(db_session, attempt.id)
    await settle_task(db_session, attempt.id)
    assert task.status == expected and task.retry_count == int(expected == "ready")
    for mock in review:
        mock.assert_not_called()
