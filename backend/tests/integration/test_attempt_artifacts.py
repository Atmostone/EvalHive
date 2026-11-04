"""Real MinIO round-trip for files retained after success, failure and cancel."""

import io
import uuid
import zipfile
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from app.auth.tokens import issue_runner_token
from app.harnesses.artifacts import collect_artifacts
from app.harnesses.attempts import create_attempt
from app.harnesses.catalog import get_manifest
from app.harnesses.executor import settle_task
from app.models.task import Task


@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled"])
async def test_attempt_files_download_after_every_outcome(
    auth_client, db_session, tmp_path, outcome
):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    task = Task(title="Artifact round trip", workspace_id=workspace_id)
    db_session.add(task)
    await db_session.flush()
    spec = {
        "protocol_version": "1.0",
        "workspace_id": str(workspace_id),
        "task_id": str(task.id),
        "attempt_id": str(uuid.uuid4()),
        "task": {"prompt": "File test"},
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
    (tmp_path / "nested").mkdir()
    name = "nested/report-✓.txt"
    (tmp_path / name).write_bytes(b"Retained engineering artifact\n")
    native = {"status": outcome, "summary": "Finished"}
    if outcome == "failed":
        native["error"] = {"code": "TEST_FAILURE", "message": "Expected engineering failure"}
    result, announcements = collect_artifacts(SimpleNamespace(workspace=tmp_path), spec, native)
    records = [
        ("run.started", {}),
        *(("artifact", record) for record in announcements),
        ("run.finished", result),
    ]
    batch = [
        {
            "protocol_version": "1.0",
            "type": "event",
            "attempt_id": str(attempt.id),
            "event_id": f"event-{seq}",
            "seq": seq,
            "source": "runner",
            "event": kind,
            "data": data,
        }
        for seq, (kind, data) in enumerate(records)
    ]
    accepted = await auth_client.post(
        f"/api/v1/attempts/{attempt.id}/events",
        json={"events": batch},
        headers={"Authorization": "Bearer " + token},
    )
    assert accepted.status_code == 200
    await settle_task(db_session, attempt.id)
    await db_session.refresh(task)
    assert task.status == ("done" if outcome == "success" else "failed")
    relative = f"attempts/{attempt.id}/{name}"
    response = await auth_client.get(f"/api/tasks/{task.id}/files/{quote(relative)}")
    assert response.status_code == 200 and response.content == b"Retained engineering artifact\n"
    assert "filename*=UTF-8''report-" in response.headers["content-disposition"]
    archive = await auth_client.get(f"/api/tasks/{task.id}/files.zip")
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
        assert zipped.namelist() == [name]
        assert zipped.read(name) == response.content
