"""Runner control acknowledgements, recovery and failure fencing."""

import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.auth.tokens import _utcnow_naive, issue_agent_token, issue_runner_token
from app.config import get_settings
from app.harnesses import executor
from app.harnesses.attempts import create_attempt
from app.harnesses.catalog import get_manifest
from app.models.task import Task
from app.models.workspace import WorkspaceMember
from evalhive_harness import response
from evalhive_runner.spool import EventSpool
from sqlalchemy import select


@pytest.fixture
async def attempt(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    task = Task(title="Runner control", workspace_id=workspace_id)
    db_session.add(task)
    await db_session.flush()
    spec = {
        "protocol_version": "1.0",
        "workspace_id": str(workspace_id),
        "task_id": str(task.id),
        "attempt_id": str(uuid.uuid4()),
        "task": {"prompt": "Control test"},
        "cwd": "/workspace",
        "config": {},
    }
    row = await create_attempt(
        db_session,
        workspace_id=workspace_id,
        task_id=task.id,
        manifest=get_manifest("pi"),
        run_spec=spec,
    )
    token = await issue_runner_token(
        db_session, attempt_id=row.id, task_id=task.id, workspace_id=workspace_id
    )
    await db_session.commit()
    return row, token


def headers(token):
    return {"Authorization": "Bearer " + token}


def events(attempt):
    common = {
        "protocol_version": "1.0",
        "type": "event",
        "attempt_id": str(attempt.id),
        "source": "test",
    }
    return [
        {**common, "event_id": "start", "seq": 0, "event": "run.started", "data": {}},
        {
            **common,
            "event_id": "end",
            "seq": 1,
            "event": "run.finished",
            "data": {"status": "success", "summary": "Done"},
        },
    ]


async def test_commands_are_idempotent_and_acknowledgements_are_immutable(auth_client, attempt):
    row, token = attempt
    command_id = str(uuid.uuid4())
    payload = {"id": command_id, "name": "feedback", "payload": {"text": "Continue"}}
    path = f"/api/attempts/{row.id}/commands"
    created = await auth_client.post(path, json=payload)
    assert created.status_code == 201 and not created.json()["duplicate"]
    repeated = await auth_client.post(path, json=payload)
    assert repeated.status_code == 201 and repeated.json()["duplicate"]
    assert (
        await auth_client.post(path, json={**payload, "payload": {"text": "Changed"}})
    ).status_code == 409
    heartbeat_path = f"/api/v1/attempts/{row.id}/heartbeat"
    heartbeat = await auth_client.post(heartbeat_path, headers=headers(token))
    assert heartbeat.status_code == 200
    assert heartbeat.json()["commands"] == [
        {"id": command_id, "name": "feedback", "payload": {"text": "Continue"}}
    ]
    assert (await auth_client.post(heartbeat_path, headers=headers(token))).json()[
        "commands"
    ] == heartbeat.json()["commands"]
    ack = response(command_id, {"accepted": True})
    ack_path = f"/api/v1/attempts/{row.id}/commands/{command_id}/ack"
    assert (await auth_client.post(ack_path, json=ack, headers=headers(token))).json() == {
        "duplicate": False
    }
    assert (await auth_client.post(ack_path, json=ack, headers=headers(token))).json() == {
        "duplicate": True
    }
    assert (
        await auth_client.post(ack_path, json=response(command_id, False), headers=headers(token))
    ).status_code == 409
    assert (await auth_client.post(heartbeat_path, headers=headers(token))).json()["commands"] == []
    stored = (await auth_client.get(path)).json()
    assert stored[0]["status"] == "acknowledged" and stored[0]["result"] == ack
    assert token not in str(stored)


async def test_queued_abort_is_not_reset_by_run_started(auth_client, attempt):
    row, token = attempt
    created = await auth_client.post(f"/api/attempts/{row.id}/commands", json={"name": "abort"})
    assert created.status_code == 201
    command_id = created.json()["id"]
    assert (await auth_client.get(f"/api/attempts/{row.id}")).json()["status"] == "stopping"
    batch = events(row)
    first = await auth_client.post(
        f"/api/v1/attempts/{row.id}/events", json={"events": batch[:1]}, headers=headers(token)
    )
    assert first.status_code == 200
    assert (await auth_client.get(f"/api/attempts/{row.id}")).json()["status"] == "stopping"
    assert (
        await auth_client.post(
            f"/api/v1/attempts/{row.id}/events", json={"events": batch[1:]}, headers=headers(token)
        )
    ).status_code == 200
    commands = (await auth_client.get(f"/api/attempts/{row.id}/commands")).json()
    assert commands[0]["status"] == "unknown"  # No invented acknowledgement.
    ack = await auth_client.post(
        f"/api/v1/attempts/{row.id}/commands/{command_id}/ack",
        json=response(command_id, {"accepted": True}),
        headers=headers(token),
    )
    assert ack.status_code == 200
    assert (
        await auth_client.post(
            f"/api/attempts/{row.id}/commands",
            json={"name": "feedback", "payload": {"text": "Late"}},
        )
    ).status_code == 409


async def test_member_can_send_feedback_but_cannot_abort(auth_client, db_session, attempt):
    row, _ = attempt
    member = await db_session.scalar(
        select(WorkspaceMember).where(WorkspaceMember.workspace_id == row.workspace_id)
    )
    member.role = "member"
    await db_session.commit()
    path = f"/api/attempts/{row.id}/commands"
    assert (
        await auth_client.post(path, json={"name": "feedback", "payload": {"text": "Continue"}})
    ).status_code == 201
    assert (await auth_client.post(path, json={"name": "abort"})).status_code == 403
    assert (await auth_client.post(f"/api/v1/attempts/{row.id}/heartbeat")).status_code == 401


@pytest.mark.parametrize("recover", [False, True])
async def test_stale_runner_replays_spool_or_records_loss(
    auth_client, db_session, attempt, tmp_path, monkeypatch, recover
):
    row, _ = attempt
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    runtime = SimpleNamespace(stop=MagicMock(return_value=True), list_active=lambda: [])
    monkeypatch.setattr(executor, "get_process_runtime", lambda: runtime)
    row.container_id = "owned-container"
    row.last_heartbeat_at = _utcnow_naive() - timedelta(seconds=120)
    await db_session.commit()
    if recover:
        path = (
            Path(tmp_path) / "workspaces" / str(row.task_id) / str(row.id) / "private/spool.sqlite"
        )
        with EventSpool(path) as spool:
            for item in events(row):
                spool.append(item)
    await executor.reconcile_attempts(db_session)
    await db_session.refresh(row)
    runtime.stop.assert_called_once_with("owned-container", str(row.workspace_id), grace_seconds=3)
    task = await db_session.get(Task, row.task_id)
    assert row.status == ("success" if recover else "lost")
    assert task.status == ("done" if recover else "failed")


async def test_failed_container_stop_is_explicit_and_does_not_leave_running(
    auth_client, db_session, attempt, tmp_path, monkeypatch
):
    row, _ = attempt
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    runtime = SimpleNamespace(
        stop=MagicMock(side_effect=OSError("Daemon unavailable")), list_active=lambda: []
    )
    monkeypatch.setattr(executor, "get_process_runtime", lambda: runtime)
    row.container_id = "owned-container"
    row.last_heartbeat_at = _utcnow_naive() - timedelta(seconds=120)
    await db_session.commit()
    await executor.reconcile_attempts(db_session)
    await db_session.refresh(row)
    assert row.status == "lost" and row.completeness["cleanup"] == "unconfirmed"
    assert (await db_session.get(Task, row.task_id)).status == "failed"


async def test_delayed_delivery_preserves_runner_observation_times(auth_client, attempt):
    row, token = attempt
    batch = events(row)
    start = _utcnow_naive() - timedelta(seconds=60)
    finish = start + timedelta(seconds=10)
    batch[0]["observed_at"] = start.isoformat() + "Z"
    batch[1]["observed_at"] = finish.isoformat() + "Z"
    response_ = await auth_client.post(
        f"/api/v1/attempts/{row.id}/events", json={"events": batch}, headers=headers(token)
    )
    assert response_.status_code == 200
    detail = (await auth_client.get(f"/api/attempts/{row.id}")).json()
    assert detail["started_at"] == start.isoformat()
    assert detail["finished_at"] == finish.isoformat()


async def test_corrupt_spool_records_loss_without_leaking_content(
    auth_client, db_session, attempt, tmp_path, monkeypatch
):
    row, _ = attempt
    attempt_id, task_id = row.id, row.task_id
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    monkeypatch.setattr(
        executor, "get_process_runtime", lambda: SimpleNamespace(list_active=lambda: [])
    )
    row.last_heartbeat_at = _utcnow_naive() - timedelta(seconds=120)
    await db_session.commit()
    path = tmp_path / "workspaces" / str(task_id) / str(attempt_id) / "private/spool.sqlite"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"Damaged journal with private native output")
    await executor.reconcile_attempts(db_session)
    await db_session.refresh(row)
    assert row.status == "lost" and row.completeness["recovery"] == "failed"
    assert "private native output" not in str(row.result)
    assert (await db_session.get(Task, task_id)).status == "failed"


async def test_recovery_preserves_command_receipt_after_terminal_event(
    auth_client, db_session, attempt, tmp_path, monkeypatch
):
    row, _ = attempt
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    monkeypatch.setattr(
        executor, "get_process_runtime", lambda: SimpleNamespace(list_active=lambda: [])
    )
    created = await auth_client.post(
        f"/api/attempts/{row.id}/commands", json={"name": "feedback", "payload": {"text": "Go"}}
    )
    command_id = created.json()["id"]
    row.last_heartbeat_at = _utcnow_naive() - timedelta(seconds=120)
    await db_session.commit()
    path = tmp_path / "workspaces" / str(row.task_id) / str(row.id) / "private/spool.sqlite"
    ack = response(command_id, {"accepted": True})
    with EventSpool(path) as spool:
        for item in events(row):
            spool.append(item)
        spool.command_started(command_id)
        spool.command_finished(command_id, ack)
    await executor.reconcile_attempts(db_session)
    await db_session.refresh(row)
    assert row.status == "success"
    stored = (await auth_client.get(f"/api/attempts/{row.id}/commands")).json()
    assert stored[0]["status"] == "acknowledged" and stored[0]["result"] == ack
    with EventSpool(path) as spool:
        assert spool.pending_count == 0 and spool.completed_commands() == []


@pytest.mark.parametrize("kind", ["webhook", "log"])
async def test_legacy_write_token_cannot_bypass_attempt_journal(
    auth_client, db_session, attempt, kind
):
    row, _ = attempt
    token = await issue_agent_token(db_session, task_id=row.task_id, workspace_id=row.workspace_id)
    await db_session.commit()
    if kind == "webhook":
        path = f"/api/v1/agent-webhook/{row.task_id}"
        payload = {
            "task_id": str(row.task_id),
            "event": "progress",
            "data": {"current_step": "fake"},
        }
    else:
        path = f"/api/v1/agent-log/{row.task_id}"
        payload = {"chunk_seq": 0, "content": "fake", "idempotency_key": "fake"}
    result = await auth_client.post(path, headers=headers(token), json=payload)
    assert result.status_code == 409
    assert (await auth_client.get(f"/api/attempts/{row.id}")).json()["next_seq"] == 0


async def test_legacy_control_routes_queue_attempt_commands(auth_client, db_session, attempt):
    row, _ = attempt
    row.container_id = "sdk-container"
    await db_session.commit()
    health = await auth_client.get("/api/agents/sdk-container/health")
    assert health.status_code == 200 and health.json()["attempt_id"] == str(row.id)
    feedback = await auth_client.post("/api/agents/sdk-container/feedback", json={"message": "Go"})
    assert feedback.status_code == 200 and feedback.json()["command_id"]
    switch = await auth_client.post(
        "/api/agents/sdk-container/switch_model", json={"model_id": "unused"}
    )
    assert switch.status_code == 422 and switch.json()["detail"]["code"] == "UNSUPPORTED_CAPABILITY"
    abort = await auth_client.post("/api/agents/sdk-container/abort", json={"reason": "Stop"})
    assert abort.status_code == 200 and abort.json()["command_id"]
    commands = (await auth_client.get(f"/api/attempts/{row.id}/commands")).json()
    assert {item["name"] for item in commands} == {"feedback", "abort"}
    assert (await auth_client.get(f"/api/attempts/{row.id}")).json()["status"] == "stopping"


async def test_restart_finishes_task_after_terminal_receipt_before_projection(
    auth_client, db_session, attempt, tmp_path, monkeypatch
):
    row, token = attempt
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    monkeypatch.setattr(
        executor, "get_process_runtime", lambda: SimpleNamespace(list_active=lambda: [])
    )
    task = await db_session.get(Task, row.task_id)
    task.status = "in_progress"
    await db_session.commit()
    accepted = await auth_client.post(
        f"/api/v1/attempts/{row.id}/events", json={"events": events(row)}, headers=headers(token)
    )
    assert accepted.status_code == 200
    row.last_heartbeat_at = _utcnow_naive() - timedelta(seconds=120)
    await db_session.commit()
    await executor.reconcile_attempts(db_session)
    await db_session.refresh(row)
    await db_session.refresh(task)
    assert row.status == "success" and task.status == "done"
    assert row.completeness["task_projection"] == "committed"
    await executor.reconcile_attempts(db_session)
    assert row.next_seq == 2
