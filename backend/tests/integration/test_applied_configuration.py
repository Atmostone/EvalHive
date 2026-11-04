"""The applied configuration is an immutable pre-execution receipt."""

import uuid

from app.auth.tokens import issue_runner_token
from app.harnesses.attempts import create_attempt, ingest_events
from app.harnesses.catalog import get_manifest
from app.models.task import Task


async def test_configuration_receipt_is_scoped_idempotent_and_precedes_execution(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    task = Task(title="Prepare", workspace_id=workspace_id)
    db_session.add(task)
    await db_session.flush()
    spec = {"protocol_version": "1.0", "workspace_id": str(workspace_id), "task_id": str(task.id),
            "attempt_id": str(uuid.uuid4()), "task": {"prompt": "Fixture"}, "cwd": "/workspace", "config": {}}
    attempt = await create_attempt(db_session, workspace_id=workspace_id, task_id=task.id, manifest=get_manifest("pi"), run_spec=spec)
    token = await issue_runner_token(db_session, attempt_id=attempt.id, task_id=task.id, workspace_id=workspace_id)
    await db_session.commit()
    path = f"/api/v1/attempts/{attempt.id}/configuration"
    headers = {"Authorization": "Bearer " + token}
    payload = {"config": {"temperature": 0}}
    assert (await auth_client.post(path, json=payload)).status_code == 401
    first = await auth_client.post(path, headers=headers, json=payload)
    assert first.status_code == 200 and first.json() == {"accepted": True, "duplicate": False}
    assert (await auth_client.post(path, headers=headers, json=payload)).json() == {"accepted": True, "duplicate": True}
    assert (await auth_client.post(path, headers=headers, json={"config": {"temperature": 1}})).status_code == 409
    await db_session.refresh(attempt)
    assert attempt.prepared_at is not None
    assert attempt.run_spec["config"] == {"temperature": 0}
    assert attempt.run_spec["metadata"]["requested_config"] == {}
    await ingest_events(db_session, attempt.id, [{"protocol_version": "1.0", "type": "event", "event_id": "start",
        "attempt_id": str(attempt.id), "seq": 0, "source": "fixture", "event": "run.started", "data": {}}])
    await db_session.commit()
    assert (await auth_client.post(path, headers=headers, json=payload)).json()["duplicate"] is True
    assert (await auth_client.post(path, headers=headers, json={"config": {}})).status_code == 409
