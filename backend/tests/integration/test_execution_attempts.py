"""Delivery retries must preserve evidence and cannot cross an attempt boundary."""

import asyncio
import copy
import uuid

import pytest
from app.auth.tokens import issue_agent_token, issue_runner_token
from app.harnesses.attempts import AttemptError, create_attempt, mark_attempt_lost
from app.harnesses.catalog import get_manifest
from app.models.execution_attempt import ExecutionEvent
from app.models.task import Task
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def make_attempt(auth_client, db_session):
    async def make(task_id=None):
        workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
        if task_id is None:
            task = Task(title="Attempt delivery test", workspace_id=workspace_id)
            db_session.add(task)
            await db_session.flush()
            task_id = task.id
        spec = {
            "protocol_version": "1.0",
            "workspace_id": str(workspace_id),
            "task_id": str(task_id),
            "attempt_id": str(uuid.uuid4()),
            "task": {"prompt": "Test delivery"},
            "cwd": "/workspace",
            "config": {},
        }
        attempt = await create_attempt(
            db_session,
            workspace_id=workspace_id,
            task_id=task_id,
            manifest=get_manifest("pi"),
            run_spec=spec,
        )
        token = await issue_runner_token(
            db_session, attempt_id=attempt.id, task_id=task_id, workspace_id=workspace_id
        )
        await db_session.commit()
        return attempt, token

    return make


def event(attempt, seq, name, data, **extra):
    return {
        "protocol_version": "1.0",
        "type": "event",
        "attempt_id": str(attempt.id),
        "event_id": f"event-{seq}",
        "seq": seq,
        "source": "test",
        "event": name,
        "data": data,
        **extra,
    }


async def send(client, attempt, token, events):
    return await client.post(
        f"/api/v1/attempts/{attempt.id}/events",
        json={"events": events},
        headers={"Authorization": "Bearer " + token},
    )


async def test_journal_preserves_interleaved_native_and_unknown_usage(
    auth_client, db_session, make_attempt
):
    attempt, token = await make_attempt()
    payloads = [
        event(attempt, 0, "run.started", {}),
        event(
            attempt,
            1,
            "tool.start",
            {"call_id": "c1", "name": "read", "arguments": {}},
            session_id="s1",
        ),
        event(
            attempt,
            2,
            "tool.start",
            {"call_id": "c2", "parent_call_id": "c1", "name": "write", "arguments": {}},
            session_id="s2",
            parent_session_id="s1",
        ),
        event(
            attempt,
            3,
            "tool.end",
            {"call_id": "c2", "result": "written", "status": "completed"},
            session_id="s2",
        ),
        event(
            attempt,
            4,
            "tool.end",
            {"call_id": "c1", "result": "read", "status": "completed"},
            session_id="s1",
        ),
        event(
            attempt,
            5,
            "usage",
            {
                "usage_id": "u1",
                "mode": "cumulative",
                "purpose": "agent",
                "model": None,
                "input_tokens": None,
                "cache_read_tokens": 0,
            },
        ),
        event(
            attempt,
            6,
            "native",
            {"name": "vendor.end", "adapter_version": "7", "payload": {"raw": True}},
        ),
        event(attempt, 7, "run.finished", {"status": "success", "summary": "Done"}),
    ]
    response = await send(auth_client, attempt, token, payloads)
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 8, "duplicates": 0, "next_seq": 8, "acked_through": 7}
    stored = await auth_client.get(f"/api/attempts/{attempt.id}/events")
    assert stored.json() == payloads
    detail = (await auth_client.get(f"/api/attempts/{attempt.id}")).json()
    assert (
        detail["status"] == "success" and detail["completeness"]["event_delivery"] == "contiguous"
    )
    assert detail["completeness"]["reasoning"] == "unknown"
    assert token not in str(detail) and "run_spec" not in detail
    finished_at = detail["finished_at"]
    replay = await send(auth_client, attempt, token, payloads)
    assert replay.status_code == 200 and replay.json()["duplicates"] == 8
    replay = await send(auth_client, attempt, token, [payloads[-1]])
    assert replay.status_code == 200 and replay.json()["duplicates"] == 1
    assert (await auth_client.get(f"/api/attempts/{attempt.id}")).json()[
        "finished_at"
    ] == finished_at
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ExecutionEvent)
            .where(ExecutionEvent.attempt_id == attempt.id)
        )
        == 8
    )
    late = await send(auth_client, attempt, token, [event(attempt, 8, "heartbeat", {})])
    assert late.status_code == 409 and late.json()["detail"]["code"] == "ATTEMPT_FINISHED"
    changed = copy.deepcopy(payloads[3])
    changed["data"]["result"] = "tampered"
    conflict = await send(auth_client, attempt, token, [changed])
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "EVENT_CONFLICT"


async def test_gap_rolls_back_whole_batch_and_ack_loss_is_recoverable(auth_client, make_attempt):
    attempt, token = await make_attempt()
    start = event(attempt, 0, "run.started", {})
    missing = event(attempt, 1, "message.end", {"message_id": "m1", "content": "answer"})
    finish = event(attempt, 2, "run.finished", {"status": "success", "summary": "Done"})
    response = await send(auth_client, attempt, token, [start, finish])
    assert response.status_code == 409 and response.json()["detail"]["code"] == "EVENT_GAP"
    assert (await auth_client.get(f"/api/attempts/{attempt.id}/events")).json() == []
    assert (await auth_client.get(f"/api/attempts/{attempt.id}")).json()["next_seq"] == 0
    # The sender loses this acknowledgement, then reconnects with its unacked spool.
    assert (await send(auth_client, attempt, token, [start, missing])).status_code == 200
    response = await send(auth_client, attempt, token, [start, missing, finish])
    assert response.json() == {"accepted": 1, "duplicates": 2, "next_seq": 3, "acked_through": 2}
    assert (
        await auth_client.get(f"/api/attempts/{attempt.id}/events?after_seq=0&limit=1")
    ).json() == [missing]


async def test_retry_fences_previous_attempt_even_for_a_valid_old_token(
    auth_client, db_session, make_attempt
):
    previous, old_token = await make_attempt()
    old_events = [
        event(previous, 0, "run.started", {}),
        event(
            previous,
            1,
            "run.finished",
            {
                "status": "failed",
                "summary": "Failed",
                "error": {"code": "TEST", "message": "Failure"},
            },
        ),
    ]
    assert (await send(auth_client, previous, old_token, old_events)).status_code == 200
    current, new_token = await make_attempt(previous.task_id)
    assert current.number == 2
    late = await send(auth_client, previous, old_token, old_events)
    assert late.status_code == 409 and late.json()["detail"]["code"] == "STALE_ATTEMPT"
    assert (
        await send(auth_client, current, old_token, [event(current, 0, "run.started", {})])
    ).status_code == 401
    assert (await send(auth_client, previous, new_token, old_events)).status_code == 401
    rows = (await auth_client.get(f"/api/tasks/{previous.task_id}/attempts")).json()
    assert [row["number"] for row in rows] == [2, 1]
    assert rows[0]["next_seq"] == 0


async def test_runner_scope_is_distinct_from_user_and_legacy_agent_tokens(
    auth_client, db_session, make_attempt
):
    attempt, token = await make_attempt()
    data = [event(attempt, 0, "run.started", {})]
    assert (
        await auth_client.post(f"/api/v1/attempts/{attempt.id}/events", json={"events": data})
    ).status_code == 401
    legacy = await issue_agent_token(
        db_session, task_id=attempt.task_id, workspace_id=attempt.workspace_id
    )
    await db_session.commit()
    assert (await send(auth_client, attempt, legacy, data)).status_code == 401
    with pytest.raises(ValueError, match="scope"):
        await issue_runner_token(
            db_session, attempt_id=attempt.id, task_id=attempt.task_id, workspace_id=uuid.uuid4()
        )
    other = await auth_client.post(
        "/api/auth/register",
        json={"email": "other-attempt@example.com", "password": "password1234"},
    )
    payload = other.json()
    headers = {
        "Authorization": "Bearer " + payload["access_token"],
        "X-Workspace-Id": payload["default_workspace_id"],
    }
    assert (
        await auth_client.get(f"/api/attempts/{attempt.id}", headers=headers)
    ).status_code == 404
    assert (
        await auth_client.get(f"/api/attempts/{attempt.id}/events", headers=headers)
    ).status_code == 404
    assert (
        await auth_client.get(f"/api/tasks/{attempt.task_id}/attempts", headers=headers)
    ).status_code == 404


async def test_concurrent_duplicate_delivery_is_serialized(auth_client, make_attempt):
    attempt, token = await make_attempt()
    records = [
        event(attempt, 0, "run.started", {}),
        event(attempt, 1, "run.finished", {"status": "success", "summary": "Done"}),
    ]
    responses = await asyncio.gather(
        send(auth_client, attempt, token, records), send(auth_client, attempt, token, records)
    )
    assert all(response.status_code == 200 for response in responses)
    assert sorted(response.json()["accepted"] for response in responses) == [0, 2]
    assert len((await auth_client.get(f"/api/attempts/{attempt.id}/events")).json()) == 2


async def test_only_one_concurrent_attempt_can_be_created(auth_client, db_session, make_attempt):
    previous, token = await make_attempt()
    await mark_attempt_lost(db_session, previous.id, "Test transport stopped")
    await db_session.commit()

    async def competing():
        async with AsyncSession(bind=db_session.bind, expire_on_commit=False) as session:
            spec = {**previous.run_spec, "attempt_id": str(uuid.uuid4())}
            try:
                attempt = await create_attempt(
                    session,
                    workspace_id=previous.workspace_id,
                    task_id=previous.task_id,
                    manifest=get_manifest("pi"),
                    run_spec=spec,
                )
                await session.commit()
                return attempt.id
            except AttemptError as error:
                assert error.code == "ATTEMPT_ACTIVE"
                return None

    results = await asyncio.gather(competing(), competing())
    assert sum(result is not None for result in results) == 1


async def test_recording_loss_is_not_invented_as_success(auth_client, db_session, make_attempt):
    attempt, token = await make_attempt()
    assert (
        await send(auth_client, attempt, token, [event(attempt, 0, "run.started", {})])
    ).status_code == 200
    await mark_attempt_lost(db_session, attempt.id, "Connector exited before a terminal event")
    await db_session.commit()
    detail = (await auth_client.get(f"/api/attempts/{attempt.id}")).json()
    assert detail["status"] == "lost" and detail["result"]["status"] == "failed"
    assert detail["completeness"]["event_delivery"] == "interrupted"
    assert len((await auth_client.get(f"/api/attempts/{attempt.id}/events")).json()) == 1


async def test_invalid_lifecycle_frames_and_payload_limits_leave_no_records(
    auth_client, make_attempt
):
    attempt, token = await make_attempt()
    invalid = [event(attempt, 0, "heartbeat", {})]
    assert (await send(auth_client, attempt, token, invalid)).status_code == 422
    bad = event(attempt, 0, "run.started", {})
    bad["protocol_version"] = "2.0"
    response = await send(auth_client, attempt, token, [bad])
    assert (
        response.status_code == 422 and response.json()["detail"]["code"] == "UNSUPPORTED_PROTOCOL"
    )
    too_big = event(attempt, 0, "run.started", {"large": "x" * (1024 * 1024)})
    assert (await send(auth_client, attempt, token, [too_big])).status_code == 422
    response = await auth_client.post(
        f"/api/v1/attempts/{attempt.id}/events",
        content=b"x" * (4 * 1024 * 1024 + 1),
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 413
    assert (await auth_client.get(f"/api/attempts/{attempt.id}/events")).json() == []
