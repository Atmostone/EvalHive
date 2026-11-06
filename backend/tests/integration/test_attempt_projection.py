"""Attempt evidence reaches existing task/log/quality APIs without invented data."""

import uuid
from decimal import Decimal

import pytest
from app.auth.tokens import issue_runner_token
from app.harnesses.attempts import create_attempt
from app.harnesses.catalog import get_manifest
from app.harnesses.executor import settle_task
from app.models.quality_record import QualityRecord
from app.models.task import Task
from app.quality.data_lake import assemble_record, build_quality_record
from app.quality.trace_cleaner import build_cleaned_trace


@pytest.mark.parametrize("foreign_artifact", [False, True])
@pytest.mark.parametrize("recovered_error", [False, True])
async def test_attempt_results_logs_trace_and_unknown_cost_are_consistent(
    auth_client, db_session, monkeypatch, foreign_artifact, recovered_error
):
    frames = []

    async def broadcast(frame):
        frames.append(frame)

    monkeypatch.setattr("app.utils.events.broadcast_log_chunk", broadcast)
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    task = Task(title="Attempt projection", workspace_id=workspace_id)
    db_session.add(task)
    await db_session.flush()
    spec = {
        "protocol_version": "1.0",
        "workspace_id": str(workspace_id),
        "task_id": str(task.id),
        "attempt_id": str(uuid.uuid4()),
        "task": {"prompt": "Projection test"},
        "cwd": "/workspace",
        "config": {},
    }
    attempt = await create_attempt(
        db_session,
        workspace_id=workspace_id,
        task_id=task.id,
        manifest=get_manifest("pi"),
        run_spec=spec,
    )
    token = await issue_runner_token(
        db_session, attempt_id=attempt.id, task_id=task.id, workspace_id=workspace_id
    )
    await db_session.commit()
    data = [
        ("run.started", {}, None),
        (
            "tool.start",
            {"call_id": "same", "name": "write", "arguments": {"path": "a.txt"}},
            "root",
        ),
        (
            "tool.start",
            {"call_id": "same", "name": "read", "arguments": {}, "parent_call_id": "same"},
            "child",
        ),
        ("tool.end", {"call_id": "same", "result": "Child", "status": "completed"}, "child"),
        ("tool.end", {"call_id": "same", "result": "Root", "status": "completed"}, "root"),
        (
            "usage",
            {
                "usage_id": "answer",
                "mode": "cumulative",
                "model": "fake",
                "purpose": "agent",
                "input_tokens": 4,
                "output_tokens": 1,
            },
            None,
        ),
        (
            "usage",
            {
                "usage_id": "answer",
                "mode": "cumulative",
                "model": "fake",
                "purpose": "agent",
                "input_tokens": 7,
                "output_tokens": 2,
            },
            None,
        ),
        (
            "artifact",
            {
                "path": "result.txt",
                "metadata": {
                    "delivery": "complete",
                    "s3_path": f"results/{task.id}/attempts/{attempt.id}/result.txt",
                },
            },
            None,
        ),
        ("run.finished", {"status": "success", "summary": "Finished"}, None),
    ]
    if foreign_artifact:
        data[-2][1]["metadata"]["s3_path"] = "results/another-task/private.txt"
    if recovered_error:
        data.insert(1, ("failure.observed", {"request_id": "request-1", "purpose": "agent",
                                           "facts": {"kind": "model_error", "status_code": 500}}, None))
    batch = [
        {
            "protocol_version": "1.0",
            "type": "event",
            "attempt_id": str(attempt.id),
            "event_id": f"event-{seq}",
            "seq": seq,
            "source": "runner" if kind == "artifact" else "test",
            "event": kind,
            "data": payload,
            **({"session_id": session} if session else {}),
        }
        for seq, (kind, payload, session) in enumerate(data)
    ]
    response = await auth_client.post(
        f"/api/v1/attempts/{attempt.id}/events",
        json={"events": batch},
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 200
    assert len(frames) == 2 and {frame["content"] for frame in frames} == {"Root", "Child"}
    assert all(frame["attempt_number"] == 1 for frame in frames)
    replay = await auth_client.post(
        f"/api/v1/attempts/{attempt.id}/events",
        json={"events": batch},
        headers={"Authorization": "Bearer " + token},
    )
    assert replay.status_code == 200 and len(frames) == 2
    await settle_task(db_session, attempt.id)
    await settle_task(db_session, attempt.id)
    await db_session.refresh(task)
    assert task.status == ("failed" if foreign_artifact else "done") and task.cost_usd is None
    if recovered_error and not foreign_artifact:
        assert task.failure_type == "llm_transient"
    assert task.token_usage["input_tokens"] == 7 and task.token_usage["output_tokens"] == 2
    assert len(task.result_files) == (0 if foreign_artifact else 1)
    if foreign_artifact:
        await db_session.refresh(attempt)
        assert attempt.completeness["artifacts"] == "invalid"
    assert (await auth_client.get(f"/api/tasks/{task.id}")).json()["cost_usd"] is None
    log = (await auth_client.get(f"/api/tasks/{task.id}/log")).json()
    assert log["attempt_id"] == str(attempt.id)
    assert [row["content"] for row in log["chunks"]] == ["Root", "Child"]
    trace = await build_cleaned_trace(db_session, task)
    assert [step["content"] for step in trace["steps"] if step["kind"] == "tool"] == [
        "Root",
        "Child",
    ]
    assert trace["stats"]["steps_result_missing"] == 0
    blob = await assemble_record(db_session, task)
    assert blob["task"]["cost_usd"] is None
    assert len(blob["execution"]["tool_calls"]) == 2
    assert blob["execution"]["attempt"]["usage"]["calls_reported"] == 1
    assert blob["execution"]["duration_seconds"] is None
    record = await build_quality_record(db_session, task)
    await db_session.refresh(record)
    assert record.cost_usd is None and record.tool_call_count == 2
    assert record.duration_seconds is None
    query = (
        await auth_client.get("/api/data-lake/query", params={"group_by": "model_used"})
    ).json()
    assert query[0]["avg_cost_usd"] is None and query[0]["cost_known_count"] == 0
    assert query[0]["avg_tokens"] == 9 and query[0]["tokens_known_count"] == 1


async def test_small_reported_cost_is_not_rounded_to_zero(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    amount = Decimal("0.000000000000000000123")
    task = Task(title="Precise execution cost", workspace_id=workspace_id, cost_usd=amount)
    db_session.add(task)
    await db_session.flush()
    record = QualityRecord(task_id=task.id, workspace_id=workspace_id, cost_usd=amount)
    db_session.add(record)
    await db_session.commit()
    await db_session.refresh(task)
    await db_session.refresh(record)
    assert task.cost_usd == amount and record.cost_usd == amount
