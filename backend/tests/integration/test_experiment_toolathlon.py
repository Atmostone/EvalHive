"""Historical benchmark results survive retirement of the core native executor."""

import copy
import uuid
from types import SimpleNamespace

import pytest
from app.models.experiment import Experiment, ExperimentRun
from app.models.task import Task
from app.orchestrator.engine import process_ready_task
from app.orchestrator.rerun import clone_task_for_rerun
from app.quality.experiments import (
    advance_experiment,
    cancel_experiment,
    clone_experiment,
    create_experiment,
    resume_experiment,
    retry_failed_experiment,
    start_experiment,
)
from sqlalchemy import func, select


def _case():
    return {
        "case_key": "historical-gym-case", "title": "Historical task",
        "external_eval": {"preprocess_command": "native preprocess", "eval_command": "native check"},
        "environment": {"required_services": ["toolathlon_pg"], "mcp_servers": ["terminal"]},
        "meta": {"task_path": "tasks/historical"},
    }


async def _history(db, workspace, *, status="completed"):
    config = {"config_key": "cfg-01", "label": "Historical", "fingerprint": "historical"}
    experiment = Experiment(
        workspace_id=workspace, name="Historical benchmark", status=status,
        dataset={"source": "benchmark_suite", "suite": "toolathlon"},
        dataset_cases=[_case()], configurations=[config],
        matrix_spec={"configurations": [config]}, n_runs_per_cell=1,
        n_toolathlon_lanes=3, eval_config={"trajectory": False},
    )
    task = Task(workspace_id=workspace, title="Historical task", status="done",
                benchmark_case_id="historical-gym-case", benchmark_suite="toolathlon-pilot")
    db.add_all([experiment, task])
    await db.flush()
    run = ExperimentRun(
        experiment_id=experiment.id, config_key="cfg-01", case_key="historical-gym-case",
        run_index=0, task_id=task.id, status="success", external_verdict=False,
        launch_time="2026-06-14 10:00:00 Saturday", lane_index=2,
        preprocess_log="historical preprocess evidence", eval_log="historical checker failure",
        preprocess_container_id="historical-preprocess", eval_container_id="historical-evaluator",
    )
    db.add(run)
    await db.commit()
    return experiment, task, run


async def test_historical_checker_and_matrix_stay_readable(auth_client, db_session):
    workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    experiment, task, run = await _history(db_session, workspace)
    original = copy.deepcopy(experiment.dataset_cases)
    detail = await auth_client.get(f"/api/experiments/{experiment.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["n_toolathlon_lanes"] == 3
    assert detail.json()["legacy_execution_retired"] is True
    checker = await auth_client.get(f"/api/quality/records/{task.id}/external-checker")
    assert checker.status_code == 200, checker.text
    assert checker.json()["available"] is True
    assert "historical checker failure" in checker.text
    assert "2026-06-14 10:00:00 Saturday" in checker.text
    await db_session.refresh(run)
    await db_session.refresh(experiment)
    assert run.external_verdict is False and run.lane_index == 2
    assert experiment.dataset_cases == original
    assert experiment.status == "completed"


@pytest.mark.parametrize("operation", [start_experiment, resume_experiment, retry_failed_experiment,
                                       cancel_experiment, clone_experiment])
async def test_historical_execution_requires_explicit_sdk_selection(
    auth_client, db_session, operation,
):
    workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    experiment, _, run = await _history(db_session, workspace, status="paused")
    original = copy.deepcopy(experiment.dataset_cases)
    with pytest.raises(ValueError, match="Legacy benchmark execution"):
        await operation(db_session, experiment)
    assert experiment.dataset_cases == original
    assert run.external_verdict is False
    assert run.preprocess_container_id == "historical-preprocess"
    assert run.eval_container_id == "historical-evaluator"
    assert await db_session.scalar(select(func.count()).select_from(Task)) == 1


async def test_old_running_matrix_pauses_without_rewriting_cells(auth_client, db_session):
    workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    experiment, _, run = await _history(db_session, workspace, status="running")
    await advance_experiment(db_session, experiment)
    assert experiment.status == "paused"
    assert "Legacy benchmark execution" in experiment.error
    assert run.status == "success" and run.external_verdict is False
    assert run.eval_log == "historical checker failure"
    assert run.eval_container_id == "historical-evaluator"
    await advance_experiment(db_session, experiment)
    assert experiment.status == "paused"


async def test_legacy_dataset_rejected_before_config_refs_or_database():
    with pytest.raises(ValueError, match="Legacy benchmark execution"):
        await create_experiment(None, workspace_id=uuid.uuid4(), payload={
            "dataset": {"source": "benchmark_suite", "suite": "toolathlon"},
        })


async def test_legacy_frozen_cases_cannot_bypass_the_sdk_boundary():
    with pytest.raises(ValueError, match="Legacy benchmark execution"):
        await create_experiment(None, workspace_id=uuid.uuid4(), payload={
            "dataset": {"source": "upload"},
        }, frozen_cases=[_case()])


async def test_historical_task_cannot_start_or_clone_through_ordinary_api(auth_client, db_session):
    workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    _, task, _ = await _history(db_session, workspace)
    for path, body in [("start", None), ("rerun", {"reuse_conditions": False})]:
        response = await auth_client.post(f"/api/tasks/{task.id}/{path}", **({"json": body} if body else {}))
        assert response.status_code == 409, response.text
        assert "SDK" in response.text
    with pytest.raises(ValueError, match="SDK run"):
        await clone_task_for_rerun(db_session, task)


async def test_legacy_ready_task_is_rejected_before_model_selection(auth_client, db_session):
    task = Task(workspace_id=uuid.UUID(auth_client.headers["X-Workspace-Id"]),
                title="Retired queued pilot", status="ready",
                run_config={"toolathlon_pilot": True})
    db_session.add(task)
    await db_session.commit()
    await process_ready_task(db_session, task)
    assert task.status == "failed" and task.current_attempt_id is None
    assert "Benchmark SDK" in task.result_summary


@pytest.mark.parametrize("command", ["_create", "_ready", "_verdict"])
async def test_retired_pilot_writes_fail_before_database_access(command, capsys):
    from app.cli import toolathlon_pilot

    with pytest.raises(SystemExit):
        await getattr(toolathlon_pilot, command)(SimpleNamespace())
    assert "SDK" in capsys.readouterr().err
