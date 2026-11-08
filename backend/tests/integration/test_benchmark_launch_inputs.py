"""Freeze selected native inputs without models, owner memory or private snapshots."""

import json
import uuid

import pytest
import pytest_asyncio
from app.benchmarks.datasets import register_dataset
from app.benchmarks.launch_inputs import refresh_authorization, resolve_inputs
from app.config import get_settings
from app.models.benchmark import BenchmarkDatasetCase, BenchmarkProfile, BenchmarkSecret
from app.models.benchmark_job import BenchmarkJob
from app.models.benchmark_trial import BenchmarkTrial
from app.models.harness_profile import HarnessProfile
from app.models.provider import LLMModel, Provider
from app.models.task import Task
from app.models.workspace import Workspace
from evalhive_benchmark import ProtocolError
from sqlalchemy import func, select
from sqlalchemy.orm import undefer

from tests.unit.test_benchmark_installations import registry as registry
from tests.unit.test_benchmark_installations import write

LIMITS = {
    "wall_time_seconds": 600,
    "memory_mb": 4096,
    "cpu_cores": 2,
    "disk_mb": 2048,
    "model_requests": 8,
}


@pytest_asyncio.fixture
async def input_selection(auth_client, registry, db_session, monkeypatch):
    root, catalog, datasets = registry
    monkeypatch.setattr(get_settings(), "benchmark_manifest_dir", str(root))
    monkeypatch.setattr(get_settings(), "benchmark_runtime_enabled", True)
    monkeypatch.setattr(get_settings(), "data_dir", str(root))
    manifest = json.loads((catalog / "manifest.json").read_text())
    manifest["secret_schema"]["required"] = ["api_key"]
    manifest["capabilities"] = {
        name: "native" for name in ("prepare", "drive", "capture", "evaluate", "cleanup")
    }
    for target in (catalog, catalog / "packages/alpha", catalog / "packages/beta"):
        write(target / "manifest.json", manifest)
    for receipt_path in catalog.glob("datasets/*.json"):
        receipt = json.loads(receipt_path.read_text())
        receipt["cases"][0]["requirements"]["capabilities"] = [
            "host.agent_api_v1",
            "model.user",
            "credential.api_key",
        ]
        receipt["cases"][0]["native_ref"] = {"private_path": "/evaluator/test.py"}
        receipt["cases"][0]["metadata"] = {"gold": "hidden-fixture-answer"}
        write(receipt_path, receipt)
        variant = "alpha" if receipt["dataset_id"].endswith("alpha") else "beta"
        write(catalog / "packages" / variant / "datasets" / receipt_path.name, receipt)
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    workspace = await db_session.get(Workspace, workspace_id)
    provider = Provider(
        workspace_id=workspace_id,
        name="Frozen fixture",
        endpoint="http://127.0.0.1:9/v1",
        api_key="synthetic-provider-secret",
    )
    db_session.add(provider)
    await db_session.flush()
    agent = LLMModel(provider_id=provider.id, display_name="Agent", api_name="fixture-agent")
    user = LLMModel(provider_id=provider.id, display_name="User", api_name="fixture-user")
    secret = BenchmarkSecret(
        workspace_id=workspace_id, name="Native service", value="synthetic-service-secret"
    )
    db_session.add_all([agent, user, secret])
    await db_session.flush()
    harness = await db_session.scalar(
        select(HarnessProfile).where(HarnessProfile.workspace_id == workspace_id)
    )
    harness.model_id = agent.id
    profile = BenchmarkProfile(
        workspace_id=workspace_id,
        name="Native fixture",
        connector_id=manifest["id"],
        connector_version=manifest["version"],
        config={},
        secret_refs={"api_key": str(secret.id)},
        model_bindings={"user": str(user.id), "unused": str(user.id)},
    )
    db_session.add(profile)
    await db_session.flush()
    selected = datasets[0]
    dataset = await register_dataset(
        db_session, workspace_id, profile, selected.dataset_id, selected.revision
    )
    case = await db_session.scalar(
        select(BenchmarkDatasetCase).where(BenchmarkDatasetCase.dataset_revision_id == dataset.id)
    )
    await db_session.commit()
    selection = {
        "profile_id": str(profile.id),
        "dataset_revision_id": str(dataset.id),
        "case_id": str(case.id),
        "harness_profile_id": str(harness.id),
    }
    return workspace, selection, profile, harness, provider


async def test_native_models_and_credentials_are_selected_from_requirements_without_public_values(
    db_session, input_selection, monkeypatch
):
    workspace, selection, _, _, _ = input_selection

    async def no_network(*args, **kwargs):
        pytest.fail("Freezing launch inputs must not call a model")

    monkeypatch.setattr("httpx.AsyncClient.request", no_network)
    frozen = await resolve_inputs(
        db_session, workspace, selection, trial_id=uuid.uuid4(), task_id=uuid.uuid4(), limits=LIMITS
    )
    private = frozen.private
    assert set(private["benchmark_spec"]["models"]) == {"user"}
    assert set(private["auxiliary_models"]) == {"user"}
    assert private["auxiliary_models"]["user"]["api_key"] == "synthetic-provider-secret"
    assert private["benchmark_private_inputs"] == {"api_key": "synthetic-service-secret"}
    assert private["benchmark_spec"]["case"]["metadata"]["gold"] == "hidden-fixture-answer"
    assert private["harness_spec"]["model"]["name"] == "fixture-agent"
    assert not private["harness_spec"].get("environment", {}).get("resources")
    assert private["harness_spec"]["metadata"]["execution_mode"] == "direct"
    public = json.dumps(frozen.snapshot)
    for hidden in (
        "synthetic-provider-secret",
        "synthetic-service-secret",
        "hidden-fixture-answer",
        "/evaluator/test.py",
    ):
        assert hidden not in public
    assert frozen.snapshot["benchmark"]["model_bindings"].keys() == {"user"}


async def test_missing_or_invalid_connections_and_unsupported_settings_fail_before_execution(
    db_session, input_selection
):
    workspace, selection, profile, harness, provider = input_selection
    workspace_id, provider_id = workspace.id, provider.id
    profile.model_bindings = {}
    await db_session.flush()
    with pytest.raises(ProtocolError) as missing:
        await resolve_inputs(
            db_session,
            workspace,
            selection,
            trial_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            limits=LIMITS,
        )
    assert missing.value.code == "SELECTION_INCOMPATIBLE"
    await db_session.rollback()
    # Reload canonical scope after rollback without consulting a caller-supplied path.
    workspace = await db_session.get(Workspace, workspace_id)
    provider = await db_session.get(Provider, provider_id)
    provider.endpoint = "https://name:synthetic-pass@example.invalid/v1"
    await db_session.flush()
    with pytest.raises(ProtocolError) as invalid:
        await resolve_inputs(
            db_session,
            workspace,
            selection,
            trial_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            limits=LIMITS,
        )
    assert invalid.value.code == "MODEL_UNAVAILABLE" and "synthetic-pass" not in str(invalid.value)
    await db_session.rollback()
    workspace = await db_session.get(Workspace, workspace_id)
    with pytest.raises((ProtocolError, ValueError)):
        await resolve_inputs(
            db_session,
            workspace,
            selection,
            trial_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            limits=LIMITS,
            benchmark_config={"uninstalledOption": True},
        )


async def test_foreign_selection_cannot_read_native_or_model_inputs(db_session, input_selection):
    workspace, selection, _, _, _ = input_selection
    selection = {**selection, "harness_profile_id": str(uuid.uuid4())}
    with pytest.raises(ProtocolError) as missing:
        await resolve_inputs(
            db_session,
            workspace,
            selection,
            trial_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            limits=LIMITS,
        )
    assert missing.value.code == "SELECTION_UNAVAILABLE"


async def test_rotation_refreshes_only_authorization_and_regrade_does_not_need_agent_connections(
    db_session, input_selection
):
    workspace, selection, _, _, provider = input_selection
    frozen = await resolve_inputs(
        db_session, workspace, selection, trial_id=uuid.uuid4(), task_id=uuid.uuid4(), limits=LIMITS
    )
    provider.api_key = "synthetic-rotated-secret"
    await db_session.flush()
    refreshed = await refresh_authorization(
        db_session, workspace.id, frozen.snapshot, frozen.private
    )
    assert refreshed["harness_credentials"]["model_api_key"] == "synthetic-rotated-secret"
    assert refreshed["auxiliary_models"]["user"]["api_key"] == "synthetic-rotated-secret"
    assert frozen.private["harness_credentials"]["model_api_key"] == "synthetic-provider-secret"
    without_agent = {**frozen.snapshot, "harness": {"model_id": str(uuid.uuid4())}}
    assert (
        await refresh_authorization(
            db_session, workspace.id, without_agent, frozen.private, evaluation_roles=[]
        )
        == frozen.private
    )
    with pytest.raises(ProtocolError) as missing:
        await refresh_authorization(db_session, workspace.id, without_agent, frozen.private)
    assert missing.value.code == "MODEL_UNAVAILABLE"
    provider.endpoint = "https://changed.example.invalid/v1"
    await db_session.flush()
    with pytest.raises(ProtocolError) as changed:
        await refresh_authorization(db_session, workspace.id, frozen.snapshot, frozen.private)
    assert changed.value.code == "MODEL_CHANGED"


async def test_http_launch_freezes_one_task_and_repeated_requests_do_not_refresh_conditions(
    auth_client, db_session, input_selection
):
    workspace, selection, profile, _, _ = input_selection
    request = {**selection, "request_id": str(uuid.uuid4()), "limits": LIMITS}
    first = await auth_client.post("/api/benchmark-trials", json=request)
    assert first.status_code == 202, first.text
    result = first.json()
    assert result["phase"] == "created" and result["job_status"] == "queued"
    assert result["execution_status"] == "pending" and result["cleanup_status"] == "pending"
    for hidden in (
        "synthetic-provider-secret",
        "synthetic-service-secret",
        "hidden-fixture-answer",
        "/evaluator/test.py",
    ):
        assert hidden not in first.text
    task = await db_session.get(Task, uuid.UUID(result["task_id"]))
    assert task.status == "ready" and task.execution_mode == "direct" and task.max_retries == 0
    assert task.run_config["benchmark_job_id"] == result["job_id"]
    profile.config = {"invalid-after-original-request": True}
    await db_session.commit()
    repeated = await auth_client.post("/api/benchmark-trials", json=request)
    assert repeated.status_code == 202 and repeated.json()["id"] == result["id"]
    assert repeated.json()["snapshot"]["benchmark"]["config"] == {}
    changed = await auth_client.post(
        "/api/benchmark-trials", json={**request, "case_id": str(uuid.uuid4())}
    )
    assert changed.status_code == 409
    assert await db_session.scalar(select(func.count()).select_from(BenchmarkJob)) == 1
    assert await db_session.scalar(select(func.count()).select_from(BenchmarkTrial)) == 1
    assert await db_session.scalar(select(func.count()).select_from(Task)) == 1
    job = await db_session.get(
        BenchmarkJob, uuid.UUID(result["job_id"]), options=[undefer(BenchmarkJob.private_launch)]
    )
    assert b"synthetic-provider-secret" not in job.private_launch


async def test_rejected_launch_cannot_leave_a_partial_task_or_trial(
    auth_client, db_session, input_selection
):
    _, selection, _, _, _ = input_selection
    response = await auth_client.post(
        "/api/benchmark-trials",
        json={
            **selection,
            "request_id": str(uuid.uuid4()),
            "harness_profile_id": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 404
    assert await db_session.scalar(select(func.count()).select_from(BenchmarkJob)) == 0
    assert await db_session.scalar(select(func.count()).select_from(BenchmarkTrial)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Task)) == 0
