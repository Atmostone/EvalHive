"""Native matrices use frozen plans and the shared product queue without a judge."""

import uuid

import pytest
from app.api.benchmark_trials import get_trial_store
from app.benchmarks.dispatch import run_execution_job
from app.benchmarks.experiment_inputs import expand
from app.models.benchmark_experiment_plan import BenchmarkExperimentPlan
from app.models.benchmark_job import BenchmarkJob
from app.models.benchmark_trial import BenchmarkTrial
from app.models.experiment import Experiment, ExperimentRun
from app.models.task import Task
from app.quality import experiments as service
from sqlalchemy import func, select

from tests.integration.test_benchmark_launch_inputs import input_selection as input_selection
from tests.unit.test_benchmark_installations import registry as registry


def payload(selection):
    return {
        "name": "Native matrix fixture",
        "dataset": {
            "source": "benchmark_sdk",
            "profile_id": selection["profile_id"],
            "dataset_revision_id": selection["dataset_revision_id"],
            "case_ids": [selection["case_id"]],
        },
        "configurations": [{"harness_profile_id": selection["harness_profile_id"]}],
        "n_runs_per_cell": 2,
    }


def no_worker(*args, **kwargs):
    pytest.fail("Cancelled native matrix must not start a worker or model")


async def test_native_matrix_uses_a_frozen_serial_queue_and_cancellation(
    auth_client, input_selection, db_session, monkeypatch
):
    workspace, selection, profile, _, _ = input_selection
    request = payload(selection)
    preview = await auth_client.post("/api/experiments/preview", json=request)
    assert preview.status_code == 200, preview.text
    assert preview.json()["total_runs"] == 2 and preview.json()["est_cost_usd"] is None
    response = await auth_client.post("/api/experiments", json=request)
    assert response.status_code == 201, response.text
    public = response.json()
    for hidden in (
        "hidden-fixture-answer",
        "synthetic-provider-secret",
        "synthetic-service-secret",
    ):
        assert hidden not in response.text
    experiment = await db_session.get(Experiment, uuid.UUID(public["id"]))
    assert await db_session.scalar(select(func.count()).select_from(BenchmarkExperimentPlan)) == 1
    assert await db_session.scalar(select(func.count()).select_from(Task)) == 0
    profile.config = {"steps": 900}
    await db_session.commit()

    def no_legacy(*args, **kwargs):
        pytest.fail("Native matrices must not inspect legacy agent images or run text judges")

    monkeypatch.setattr(service, "_agent_image_ids", no_legacy)
    monkeypatch.setattr(service, "_evaluate_child", no_legacy)
    await service.start_experiment(db_session, experiment)
    await service.advance_experiment(db_session, experiment)
    await service.advance_experiment(db_session, experiment)
    jobs = list(await db_session.scalars(select(BenchmarkJob)))
    assert len(jobs) == 1 and jobs[0].status == "queued"
    trial = await db_session.get(BenchmarkTrial, jobs[0].trial_id)
    assert trial.snapshot["benchmark"]["config"] == {}
    task = await db_session.get(Task, trial.task_id)
    assert task.origin == "experiment" and task.benchmark_suite == "exp:" + str(experiment.id)
    runs = list(await db_session.scalars(select(ExperimentRun).order_by(ExperimentRun.run_index)))
    assert [row.status for row in runs] == ["running", "pending"]
    assert len(runs[0].condition_fingerprint) == 32
    await service.pause_experiment(db_session, experiment)
    await service.advance_experiment(db_session, experiment)
    assert await db_session.scalar(select(func.count()).select_from(BenchmarkJob)) == 1
    await service.resume_experiment(db_session, experiment)
    await service.cancel_experiment(db_session, experiment)
    assert experiment.status == "cancelled" and experiment.completed_at is None
    await run_execution_job(get_trial_store(), jobs[0].id, runtime_factory=no_worker)
    await db_session.refresh(trial)
    assert trial.execution_status == "cancelled" and trial.cleanup_status == "complete"
    await service.advance_experiment(db_session, experiment)
    await db_session.refresh(experiment)
    await db_session.refresh(runs[0])
    await db_session.refresh(runs[1])
    assert experiment.completed_at is not None
    assert runs[0].status == "failed" and runs[1].status == "skipped"
    assert runs[0].weighted_score is None and runs[0].external_verdict is None
    for suffix in ("", "/results"):
        response = await auth_client.get(f"/api/experiments/{experiment.id}{suffix}")
        assert response.status_code == 200, response.text
        for hidden in (
            "hidden-fixture-answer",
            "synthetic-provider-secret",
            "synthetic-service-secret",
        ):
            assert hidden not in response.text


@pytest.mark.parametrize(
    "change",
    [
        {"configurations": [{"orchestrator": True}]},
        {"max_parallel": 2},
        {"eval_config": {"mode": "judge"}},
        {"budget_limit_usd": 1},
    ],
)
async def test_unsupported_native_matrix_settings_fail_without_partial_rows(
    auth_client, input_selection, db_session, change
):
    _, selection, _, _, _ = input_selection
    response = await auth_client.post("/api/experiments", json={**payload(selection), **change})
    assert response.status_code == 400, response.text
    for model in (Experiment, BenchmarkExperimentPlan, BenchmarkTrial, BenchmarkJob, Task):
        assert await db_session.scalar(select(func.count()).select_from(model)) == 0


def test_native_axes_deduplicate_and_reject_unsupported_or_unbounded_inputs():
    profile = str(uuid.uuid4())
    configs = expand(
        [], {"harness_profile_id": [profile, profile], "harness_config": [{}, {"level": 2}]}
    )
    assert len(configs) == 2 and configs[0]["config_key"] == "cfg-01"
    for axes in (
        {"template_id": [profile]},
        {"harness_profile_id": [profile] * 25},
        {"harness_profile_id": []},
    ):
        with pytest.raises(ValueError):
            expand([], axes)
