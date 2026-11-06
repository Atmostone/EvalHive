"""A recorded rerun cannot replace archive evidence or silently change its harness."""

import json
import uuid
from types import SimpleNamespace

import pytest
from app.auth.tokens import issue_runner_token
from app.harnesses.attempts import (
    AttemptError,
    create_attempt,
    ingest_events,
    prepare_configuration,
)
from app.harnesses.executor import settle_task
from app.harnesses.launch import launch_task, resolve_launch
from app.models.harness_profile import HarnessProfile
from app.models.provider import LLMModel, Provider
from app.models.quality_record import QualityRecord
from app.models.task import Task
from app.quality.bundle import _fetch_attempt_journals, blob_digests, check_blobs
from sqlalchemy import select


async def archived_fixture(auth_client, db):
    workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    provider = Provider(workspace_id=workspace, name="Fixture", endpoint="http://fixture.invalid/v1", api_key="private-fixture-key")
    db.add(provider)
    await db.flush()
    model = LLMModel(provider_id=provider.id, display_name="Fixture", api_name="fixture")
    db.add(model)
    await db.flush()
    profile = await db.scalar(select(HarnessProfile).where(HarnessProfile.workspace_id == workspace, HarnessProfile.is_default.is_(True)))
    profile.model_id, profile.config = model.id, {"temperature": 0}
    task = Task(workspace_id=workspace, title="Original", description="Fixture", execution_mode="direct", run_config={"memory_mode": "off"})
    db.add(task)
    await db.flush()
    resolved = await resolve_launch(db, task)
    resolved.spec["metadata"]["image_id"] = "sha256:" + "1" * 64
    attempt = await create_attempt(db, workspace_id=workspace, task_id=task.id, manifest=resolved.manifest, run_spec=resolved.spec, profile_id=profile.id)
    await prepare_configuration(db, attempt.id, {"temperature": 0})
    token = await issue_runner_token(db, attempt_id=attempt.id, task_id=task.id, workspace_id=workspace)
    frames = [{"protocol_version": "1.0", "type": "event", "attempt_id": str(attempt.id), "event_id": str(seq),
               "seq": seq, "source": "fixture", "event": name, "data": data} for seq, (name, data) in enumerate([
                   ("run.started", {}), ("message.end", {"message_id": "answer", "content": "Fixture"}),
                   ("run.finished", {"status": "success", "summary": "Fixture complete"})])]
    await ingest_events(db, attempt.id, frames)
    await db.commit()
    await settle_task(db, attempt.id)
    record = await db.scalar(select(QualityRecord).where(QualityRecord.task_id == task.id))
    assert record is not None and record.execution_attempt_id == attempt.id
    return task, attempt, profile, record, token


async def test_archived_source_is_immutable_and_new_conditions_create_a_linked_draft(auth_client, db_session):
    task, attempt, _, record, _ = await archived_fixture(auth_client, db_session)
    assert (await auth_client.get(f"/api/tasks/{task.id}")).json()["results_archived"]
    assert (await auth_client.post(f"/api/tasks/{task.id}/start")).status_code == 409
    assert (await auth_client.patch(f"/api/tasks/{task.id}", json={"status": "ready"})).status_code == 409
    with pytest.raises(AttemptError, match="Archived results"):
        spec = {**attempt.run_spec, "attempt_id": str(uuid.uuid4())}
        await create_attempt(db_session, workspace_id=task.workspace_id, task_id=task.id, manifest=attempt.manifest, run_spec=spec)
    created = await auth_client.post(f"/api/tasks/{task.id}/rerun", json={"reuse_conditions": False, "execution_mode": "direct"})
    assert created.status_code == 201, created.text
    clone = await db_session.get(Task, uuid.UUID(created.json()["id"]))
    assert clone.replay_of_task_id == task.id and clone.status == "backlog"
    assert clone.run_config["new_harness_conditions"] is True
    await db_session.refresh(record)
    assert record.execution_attempt_id == attempt.id and record.cost_usd is None


async def test_recorded_replay_preserves_settings_and_rejects_a_rebuilt_image(auth_client, db_session, monkeypatch):
    task, _, profile, _, _ = await archived_fixture(auth_client, db_session)
    profile.config = {"temperature": 1}
    await db_session.commit()
    response = await auth_client.post(f"/api/quality/records/{task.id}/replay")
    assert response.status_code == 200, response.text
    clone = await db_session.get(Task, uuid.UUID(response.json()["replay_task_id"]))
    resolved = await resolve_launch(db_session, clone)
    assert resolved.spec["config"] == {"temperature": 0}
    assert resolved.spec["task"]["prompt"] == "Original\n\nFixture"
    assert clone.execution_mode == "direct"
    monkeypatch.setattr("app.harnesses.executor.get_process_runtime", lambda: SimpleNamespace(resolve_image=lambda *args: "sha256:" + "2" * 64))
    with pytest.raises(AttemptError, match="Recorded image"):
        await launch_task(db_session, clone)
    assert clone.current_attempt_id is None


async def test_attempt_journal_export_preserves_events_and_detects_missing_or_corrupt_evidence(auth_client, db_session):
    _, attempt, _, record, _ = await archived_fixture(auth_client, db_session)
    blobs = await _fetch_attempt_journals(db_session, [record])
    name = f"blobs/attempts/{attempt.id}.jsonl"
    rows = [json.loads(line) for line in blobs[name].splitlines()]
    assert rows[0]["attempt"]["connector_id"] == "pi"
    assert [row["payload"]["seq"] for row in rows[1:]] == [0, 1, 2]
    assert "private-fixture-key" not in blobs[name].decode()
    manifest = {"blobs": blob_digests(blobs), "counts": {"expected_attempt_journals": 1}}
    assert check_blobs(blobs, manifest) == []
    assert check_blobs({}, manifest)
    assert check_blobs({name: blobs[name] + b"corrupt"}, manifest)


async def test_rejection_after_archiving_creates_a_new_revision_without_reusing_the_record(auth_client, db_session):
    task, attempt, _, record, _ = await archived_fixture(auth_client, db_session)
    task.status, record.final_status = "awaiting_approval", "awaiting_approval"
    await db_session.commit()
    response = await auth_client.patch(f"/api/tasks/{task.id}/reject", json={"feedback": "Add the missing file"})
    assert response.status_code == 200, response.text
    assert response.json()["id"] != str(task.id)
    clone = await db_session.get(Task, uuid.UUID(response.json()["id"]))
    assert clone.replay_of_task_id == task.id and clone.status == "ready"
    resolved = await resolve_launch(db_session, clone)
    assert "Add the missing file" in resolved.spec["task"]["prompt"]
    await db_session.refresh(record)
    assert record.execution_attempt_id == attempt.id


async def test_current_trace_does_not_include_an_earlier_attempts_orchestration(auth_client, db_session):
    from app.models.event import AgentEvent
    from app.quality.data_lake import assemble_record
    from app.quality.trace_cleaner import build_cleaned_trace

    task, attempt, _, _, _ = await archived_fixture(auth_client, db_session)
    db_session.add_all([
        AgentEvent(workspace_id=task.workspace_id, task_id=task.id, source="orchestrator", event_type="orchestrator_reasoning",
                   data={"reasoning": "Earlier attempt private reasoning", "attempt_id": str(uuid.uuid4())}),
        AgentEvent(workspace_id=task.workspace_id, task_id=task.id, source="orchestrator", event_type="orchestrator_reasoning",
                   data={"reasoning": "Unscoped historical reasoning"}),
        AgentEvent(workspace_id=task.workspace_id, task_id=task.id, source="runner", event_type="agent_completed",
                   data={"message": "Current completion", "attempt_id": str(attempt.id)}),
    ])
    await db_session.commit()
    trace = await build_cleaned_trace(db_session, task)
    record = await assemble_record(db_session, task)
    assert "Earlier attempt private reasoning" not in str(trace)
    assert "Unscoped historical reasoning" not in str(trace)
    assert "Earlier attempt private reasoning" not in str(record)
    assert record["execution"]["harness"]["connector_id"] == "pi"


async def test_matrix_native_settings_validate_against_the_inherited_workspace_profile(auth_client, db_session):
    from app.quality.experiments import _validate_config_refs
    from evalhive_harness import ProtocolError

    workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await _validate_config_refs(db_session, workspace, [{"harness_config": {"temperature": 0}}])
    with pytest.raises(ProtocolError) as rejected:
        await _validate_config_refs(db_session, workspace, [{"harness_config": {"unavailableOption": True}}])
    assert rejected.value.code == "INVALID_CONFIG"
