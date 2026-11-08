"""Matrix retries archive old cell selections while native trials remain immutable."""

import json
import uuid
from copy import deepcopy

import pytest
import pytest_asyncio
from app.api.benchmark_trials import get_trial_store
from app.benchmarks.dispatch import run_execution_job
from app.benchmarks.trials import failure
from app.config import get_settings
from app.harnesses.catalog import get_manifest
from app.models.benchmark_job import BenchmarkJob
from app.models.benchmark_trial import BenchmarkTrial
from app.models.experiment import Experiment, ExperimentAttempt, ExperimentRun
from app.models.task import Task
from app.quality import experiments as service
from sqlalchemy import func, select

from tests.integration.test_benchmark_experiments import payload
from tests.integration.test_benchmark_launch_inputs import input_selection as input_selection
from tests.unit.test_benchmark_installations import registry as registry


@pytest_asyncio.fixture
async def failed_matrix(auth_client, input_selection, db_session, monkeypatch, tmp_path, request):
    _, selection, _, _, _ = input_selection
    manifest = get_manifest("pi")
    if getattr(request, "param", True):
        manifest["requirements"]["runtime_bundle"]["image"] = "sha256:" + "3" * 64
    root = tmp_path / "harnesses" / "pi"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(get_settings(), "harness_manifest_dir", str(root.parent))
    response = await auth_client.post(
        "/api/experiments", json={**payload(selection), "n_runs_per_cell": 1}
    )
    assert response.status_code == 201, response.text
    experiment = await db_session.get(Experiment, uuid.UUID(response.json()["id"]))
    await service.start_experiment(db_session, experiment)
    await service.advance_experiment(db_session, experiment)
    job = await db_session.scalar(select(BenchmarkJob))

    def unavailable(*args, **kwargs):
        raise failure("INSTALLATION_UNAVAILABLE", "Synthetic unavailable worker")

    store = get_trial_store()
    await run_execution_job(store, job.id, runtime_factory=unavailable)
    await service.advance_experiment(db_session, experiment)
    await db_session.refresh(experiment)
    row = await db_session.scalar(select(ExperimentRun))
    trial = await db_session.get(BenchmarkTrial, job.trial_id, populate_existing=True)
    assert experiment.status == "failed" and row.status == "failed"
    return store, experiment, row, trial


async def test_native_failed_cell_retry_keeps_history_and_binds_new_trial_lineage(
    auth_client,
    db_session,
    failed_matrix,
    input_selection,
):
    _, experiment, row, source = failed_matrix
    old_task_id, source_identity = row.task_id, (source.next_seq, deepcopy(source.snapshot))
    condition = row.condition_fingerprint
    input_selection[2].config = {"steps": 900}
    await db_session.commit()
    response = await auth_client.post(f"/api/experiments/{experiment.id}/retry-failed")
    assert response.status_code == 202 and response.json()["retried"] == 1, response.text
    await db_session.refresh(experiment)
    await db_session.refresh(row)
    assert experiment.status == "running" and experiment.revision == 2
    assert row.status == "pending" and row.task_id is None and row.attempt_count == 1
    history_response = await auth_client.get(
        f"/api/experiments/{experiment.id}/cells/{row.id}/attempts"
    )
    assert history_response.status_code == 200, history_response.text
    assert history_response.json()[0]["task_id"] == str(old_task_id)
    assert history_response.json()[0]["task_available"] is True
    assert history_response.json()[0]["cost_usd"] is None
    history = await db_session.scalar(select(ExperimentAttempt))
    assert (
        history.task_id == old_task_id and history.status == "failed" and history.attempt_index == 1
    )
    assert history.condition_fingerprint == condition and history.cost_usd is None
    assert (
        await db_session.get(Task, old_task_id, populate_existing=True)
    ).benchmark_suite.endswith(":retired")
    await service.advance_experiment(db_session, experiment)
    await db_session.refresh(row)
    assert row.status == "running" and row.attempt_count == 2 and row.task_id != old_task_id
    task = await db_session.get(Task, row.task_id)
    repeated = await db_session.get(
        BenchmarkTrial, uuid.UUID(task.run_config["benchmark_trial_id"])
    )
    assert repeated.retry_of_id == source.id and task.replay_of_task_id == old_task_id
    assert (
        repeated.snapshot == source_identity[1] and repeated.snapshot["benchmark"]["config"] == {}
    )
    assert row.condition_fingerprint == condition
    await db_session.refresh(source)
    assert (source.next_seq, source.snapshot) == source_identity and source.phase == "finished"
    assert (
        await auth_client.post(f"/api/experiments/{experiment.id}/retry-failed")
    ).status_code == 409
    assert await db_session.scalar(select(func.count()).select_from(ExperimentAttempt)) == 1


@pytest.mark.parametrize("failed_matrix", [False], indirect=True)
async def test_unpinned_native_matrix_cannot_claim_an_exact_retry(
    auth_client, db_session, failed_matrix
):
    _, experiment, row, _ = failed_matrix
    response = await auth_client.post(f"/api/experiments/{experiment.id}/retry-failed")
    assert response.status_code == 409 and "not pinned" in response.text, response.text
    await db_session.refresh(row)
    assert row.status == "failed" and row.task_id is not None
    assert await db_session.scalar(select(func.count()).select_from(ExperimentAttempt)) == 0


async def test_native_retry_requires_a_settled_or_paused_matrix(
    auth_client, db_session, input_selection
):
    _, selection, _, _, _ = input_selection
    response = await auth_client.post("/api/experiments", json=payload(selection))
    assert response.status_code == 201
    experiment = await db_session.get(Experiment, uuid.UUID(response.json()["id"]))
    await service.start_experiment(db_session, experiment)
    assert (
        await auth_client.post(f"/api/experiments/{experiment.id}/retry-failed")
    ).status_code == 409
    assert await db_session.scalar(select(func.count()).select_from(Task)) == 0


async def test_cell_history_marks_missing_execution_and_remains_scoped(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    experiment = Experiment(
        workspace_id=workspace_id,
        name="Historical missing execution",
        dataset={"source": "upload"},
        dataset_cases=[],
        configurations=[],
        matrix_spec={},
    )
    db_session.add(experiment)
    await db_session.flush()
    row = ExperimentRun(
        experiment_id=experiment.id,
        config_key="historical",
        case_key="missing",
        run_index=0,
        status="failed",
    )
    db_session.add(row)
    await db_session.flush()
    db_session.add(
        ExperimentAttempt(
            experiment_run_id=row.id,
            attempt_index=1,
            status="failed",
            task_id=None,
            cost_usd=None,
            retired_reason="retry",
        )
    )
    await db_session.commit()
    base = f"/api/experiments/{experiment.id}/cells/{row.id}/attempts"
    response = await auth_client.get(base)
    assert response.status_code == 200, response.text
    saved = response.json()[0]
    assert saved["task_id"] is None and saved["task_available"] is False
    assert saved["external_verdict"] is None and saved["cost_usd"] is None
    assert (
        await auth_client.get(f"/api/experiments/{uuid.uuid4()}/cells/{row.id}/attempts")
    ).status_code == 404
    assert (
        await auth_client.get(f"/api/experiments/{experiment.id}/cells/{uuid.uuid4()}/attempts")
    ).status_code == 404
    assert (await auth_client.get(base + "?limit=501")).status_code == 422
