"""Direct work resolves a profile without invoking orchestration or evaluation."""

import uuid

import pytest
from app.harnesses.attempts import create_attempt
from app.harnesses.catalog import get_manifest
from app.harnesses.launch import resolve_launch
from app.models.harness_profile import HarnessProfile
from app.models.provider import LLMModel, Provider
from app.models.task import Task
from app.models.template import Template
from sqlalchemy import select


async def configured_profile(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    provider = Provider(workspace_id=workspace_id, name="Local fixture", endpoint="http://local.fixture/v1", api_key="fixture-only-key")
    db_session.add(provider)
    await db_session.flush()
    model = LLMModel(provider_id=provider.id, display_name="Fixture", api_name="fixture")
    db_session.add(model)
    await db_session.flush()
    profile = await db_session.scalar(select(HarnessProfile).where(
        HarnessProfile.workspace_id == workspace_id, HarnessProfile.is_default.is_(True)
    ))
    profile.model_id = model.id
    await db_session.commit()
    return profile


async def test_direct_start_requires_a_model_before_queueing(auth_client, db_session):
    created = await auth_client.post("/api/tasks", json={"title": "Direct fixture", "execution_mode": "direct"})
    task_id = created.json()["id"]
    rejected = await auth_client.post(f"/api/tasks/{task_id}/start")
    assert rejected.status_code == 422, rejected.text
    assert "model connection" in rejected.text
    assert (await auth_client.get(f"/api/tasks/{task_id}")).json()["status"] == "backlog"
    await configured_profile(auth_client, db_session)
    started = await auth_client.post(f"/api/tasks/{task_id}/start")
    assert started.status_code == 200 and started.json()["status"] == "ready"
    assert started.json()["orchestrator_usage"] == {}


async def test_profile_precedence_and_explicit_empty_role_tools(auth_client, db_session):
    default = await configured_profile(auth_client, db_session)
    role_profile = HarnessProfile(workspace_id=default.workspace_id, name="Role", connector_id="pi", connector_version=get_manifest("pi")["version"], model_id=default.model_id)
    explicit = HarnessProfile(workspace_id=default.workspace_id, name="Explicit", connector_id="opencode", connector_version=get_manifest("opencode")["version"], model_id=default.model_id)
    db_session.add_all([role_profile, explicit])
    await db_session.flush()
    role = Template(workspace_id=default.workspace_id, name="Optional role", description="Fixture", soul_md="Fixture role", harness_profile_id=role_profile.id, execution_mode="direct", tool_ids=[])
    task = Task(workspace_id=default.workspace_id, title="Resolve work", execution_mode="direct")
    db_session.add_all([role, task])
    await db_session.flush()
    no_role = await resolve_launch(db_session, task)
    assert no_role.profile.id == default.id
    assert not any(resource["kind"] == "tools" for resource in no_role.spec["environment"]["resources"])
    task.template_id = role.id
    with_role = await resolve_launch(db_session, task)
    assert with_role.profile.id == role_profile.id
    assert with_role.spec["instructions"] == ["Fixture role"]
    toolset = next(resource for resource in with_role.spec["environment"]["resources"] if resource["kind"] == "tools")
    assert toolset["config"]["names"] == []
    task.harness_profile_id = explicit.id
    applied = await resolve_launch(db_session, task)
    assert applied.profile.id == explicit.id and applied.manifest["id"] == "opencode"
    assert "fixture-only-key" not in str(applied.spec)
    assert applied.credentials["model_api_key"] == "fixture-only-key"
    assert applied.spec["environment"]["artifact_policy"]["root"] == "."


async def test_direct_ready_path_does_not_call_orchestrator_or_quality(auth_client, db_session, monkeypatch):
    from app.orchestrator.engine import process_ready_task

    profile = await configured_profile(auth_client, db_session)
    task = Task(workspace_id=profile.workspace_id, title="Direct", status="ready", execution_mode="direct")
    db_session.add(task)
    await db_session.commit()
    calls = []

    async def forbidden(*args, **kwargs):
        raise AssertionError("Direct execution called an orchestration model")

    async def launch(db, task, template=None):
        resolved = await resolve_launch(db, task, template)
        calls.append(resolved)

    monkeypatch.setattr("app.orchestrator.engine.resolve_workspace_model", forbidden)
    monkeypatch.setattr("app.orchestrator.engine.decide_decomposition", forbidden)
    monkeypatch.setattr("app.orchestrator.llm.evaluate_agent_result", forbidden)
    monkeypatch.setattr("app.harnesses.launch.launch_task", launch)
    await process_ready_task(db_session, task)
    assert len(calls) == 1 and calls[0].spec["metadata"]["execution_mode"] == "direct"


async def test_active_attempt_fences_reconfiguration_and_start(auth_client, db_session):
    profile = await configured_profile(auth_client, db_session)
    task = Task(workspace_id=profile.workspace_id, title="Active", execution_mode="direct", harness_profile_id=profile.id)
    db_session.add(task)
    await db_session.flush()
    resolved = await resolve_launch(db_session, task)
    attempt = await create_attempt(db_session, workspace_id=task.workspace_id, task_id=task.id,
        manifest=get_manifest("pi"), run_spec=resolved.spec, profile_id=profile.id)
    await db_session.commit()
    for body in ({"status": "ready"}, {"execution_mode": "orchestrated"}, {"harness_profile_id": None}):
        response = await auth_client.patch(f"/api/tasks/{task.id}", json=body)
        assert response.status_code == 409
    assert (await auth_client.post(f"/api/tasks/{task.id}/start")).status_code == 409
    current = (await auth_client.get(f"/api/tasks/{task.id}")).json()
    assert current["current_attempt_id"] == str(attempt.id)


async def test_direct_selection_rejects_foreign_profile(auth_client, db_session):
    profile = await configured_profile(auth_client, db_session)
    from app.harnesses.profiles import ProfileError

    task = Task(workspace_id=profile.workspace_id, title="Scoped", execution_mode="direct", harness_profile_id=uuid.uuid4())
    with pytest.raises(ProfileError, match="unavailable"):
        await resolve_launch(db_session, task)
