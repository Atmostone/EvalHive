"""Native aggregation uses exact private results and durable SDK command receipts."""

from copy import deepcopy

import pytest
from app.benchmarks.native_aggregation import NativeAggregation, population_conditions
from app.models.benchmark_command import BenchmarkCommand
from app.models.benchmark_trial import BenchmarkJournalEntry
from evalhive_benchmark.peer import identity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from tests.integration.test_benchmark_driver import driver as driver

SNAPSHOT = {
    "selection": {"case_id": "one", "dataset_revision_id": "revision"},
    "dataset": {
        "case": {"dataset_id": "fixture", "revision": "1", "case_id": "one"},
        "case_source_digest": "case-digest",
        "source_digest": "dataset-digest",
    },
    "benchmark": {"id": "fixture.driver", "version": "1.0", "worker_image": "sha256:" + "a" * 64},
    "harness": {"model": "fixture"},
}


async def receipt(controller):
    async with AsyncSession(controller.store.engine) as db:
        row = await db.scalar(
            select(BenchmarkJournalEntry)
            .where(
                BenchmarkJournalEntry.trial_id == controller.owner.trial_id,
                BenchmarkJournalEntry.event.in_(["aggregate.completed", "aggregate.unavailable"]),
            )
            .options(undefer(BenchmarkJournalEntry.private_data))
        )
        return row, controller.store.private.open("journal:" + str(row.id), row.private_data)


@pytest.mark.parametrize("driver", [{"snapshot": SNAPSHOT}], indirect=True)
async def test_native_aggregate_has_frozen_population_and_confirmed_command(driver):
    controller, spec = driver
    controller.aggregation = NativeAggregation(
        controller.store, controller.owner, controller.host.manifest
    )
    result = await controller.run(spec)
    assert result["execution"] == "success" and result["evaluation"] == "completed"
    row, value = await receipt(controller)
    assert row.event == "aggregate.completed" and value["result"]["metrics"][0]["value"] == 1
    assert value["payload"]["expected"] == 1 and len(value["members"]) == 1
    assert value["conditions_digest"] == identity(population_conditions(SNAPSHOT))
    assert value["result_digest"] == identity(value["result"])
    async with AsyncSession(controller.store.engine) as db:
        command = await db.scalar(
            select(BenchmarkCommand).where(BenchmarkCommand.request_id == value["request_id"])
        )
        assert command.method == "aggregate" and command.state == "completed"
    assert b"aggregate-private-native" not in row.private_data


@pytest.mark.parametrize("driver", [{"snapshot": SNAPSHOT}], indirect=True)
async def test_unavailable_aggregate_does_not_replace_the_native_verdict(driver):
    controller, spec = driver
    manifest = deepcopy(controller.host.manifest)
    manifest["capabilities"]["aggregate"] = "unsupported"
    controller.aggregation = NativeAggregation(controller.store, controller.owner, manifest)
    result = await controller.run(spec)
    row, value = await receipt(controller)
    assert row.event == "aggregate.unavailable" and value["reason"] == "UNSUPPORTED_CAPABILITY"
    assert result["evaluation"] == "completed" and result["recording"] == "complete"
    assert value["members"][0]["result_digest"] == identity(value["payload"]["results"][0])


@pytest.mark.parametrize("driver", [{"snapshot": SNAPSHOT}], indirect=True)
async def test_native_aggregate_failure_retains_checker_success_and_explicit_reason(driver):
    controller, spec = driver
    spec["config"] = {"fail_phase": "aggregate"}
    controller.aggregation = NativeAggregation(
        controller.store, controller.owner, controller.host.manifest
    )
    result = await controller.run(spec)
    row, value = await receipt(controller)
    assert row.event == "aggregate.unavailable" and value["reason"] == "CONNECTOR_COMMAND_FAILED"
    assert result["evaluation"] == "completed" and result["cleanup"] == "complete"


def test_population_identity_keeps_versions_models_and_configurations():
    other = deepcopy(SNAPSHOT)
    other["selection"]["case_id"] = "two"
    other["dataset"]["case"]["case_id"] = "two"
    other["dataset"]["case_source_digest"] = "another-case"
    assert population_conditions(other) == population_conditions(SNAPSHOT)
    for target, field in (
        ("benchmark", "worker_image"),
        ("benchmark", "version"),
        ("dataset", "source_digest"),
        ("harness", "model"),
    ):
        changed = deepcopy(other)
        changed[target][field] = "changed"
        assert population_conditions(changed) != population_conditions(SNAPSHOT)


@pytest.mark.parametrize("driver", [{"snapshot": SNAPSHOT}], indirect=True)
async def test_public_aggregate_requires_receipt_and_workspace_membership(
    driver, auth_client, db_session, monkeypatch
):
    import uuid

    from app.api.benchmark_trials import get_trial_store
    from app.benchmarks.aggregate_read import project, read_records
    from app.main import app
    from app.models.user import User
    from app.models.workspace import WorkspaceMember

    controller, spec = driver
    controller.aggregation = NativeAggregation(
        controller.store, controller.owner, controller.host.manifest
    )
    await controller.run(spec)
    monkeypatch.setitem(app.dependency_overrides, get_trial_store, lambda: controller.store)
    user = await db_session.scalar(select(User).where(User.email != "admin@local"))
    db_session.add(
        WorkspaceMember(user_id=user.id, workspace_id=controller.owner.workspace_id, role="viewer")
    )
    await db_session.commit()
    personal = auth_client.headers["X-Workspace-Id"]
    trial_id = controller.owner.trial_id
    assert (
        await auth_client.get(f"/api/benchmark-trials/{trial_id}/aggregates")
    ).status_code == 404
    assert await read_records(db_session, controller.store, uuid.UUID(personal), trial_id) == []
    auth_client.headers["X-Workspace-Id"] = str(controller.owner.workspace_id)
    response = await auth_client.get(f"/api/benchmark-trials/{trial_id}/aggregates")
    assert response.status_code == 200, response.text
    public = response.json()
    assert len(public) == 1 and public[0]["metrics"][0]["value"] == 1
    assert public[0]["complete"] and public[0]["population_digest"]
    assert "aggregate-private-native" not in response.text and "payload" not in response.text
    assert (
        await auth_client.get(f"/api/benchmark-trials/{trial_id}/aggregates?offset=1")
    ).json() == []
    assert (
        await auth_client.get(f"/api/benchmark-trials/{trial_id}/aggregates?limit=21")
    ).status_code == 422
    async with AsyncSession(controller.store.engine) as db:
        records = await read_records(db, controller.store, controller.owner.workspace_id, trial_id)
        assert project(*records[0])["evaluation_id"] == public[0]["evaluation_id"]


@pytest.mark.parametrize("driver", [{"snapshot": SNAPSHOT}], indirect=True)
async def test_trial_archives_separate_public_records_from_authorized_native_recompute(
    driver, auth_client, db_session, monkeypatch
):
    from app.api.benchmark_trials import get_trial_store
    from app.main import app
    from app.models.user import User
    from app.models.workspace import WorkspaceMember
    from evalhive_benchmark.archive import recompute_archive, verify_archive
    from sqlalchemy import update

    controller, spec = driver
    controller.aggregation = NativeAggregation(
        controller.store, controller.owner, controller.host.manifest
    )
    await controller.run(spec)
    monkeypatch.setitem(app.dependency_overrides, get_trial_store, lambda: controller.store)
    user = await db_session.scalar(select(User).where(User.email != "admin@local"))
    membership = WorkspaceMember(
        user_id=user.id, workspace_id=controller.owner.workspace_id, role="viewer"
    )
    db_session.add(membership)
    await db_session.commit()
    auth_client.headers["X-Workspace-Id"] = str(controller.owner.workspace_id)
    endpoint = f"/api/benchmark-trials/{controller.owner.trial_id}/archive"
    response = await auth_client.get(endpoint)
    assert response.status_code == 200, response.text
    assert not verify_archive(response.json())["native_recompute_inputs"]
    assert "aggregate-private-native" not in response.text
    assert (await auth_client.get(endpoint + "?include_native_inputs=true")).status_code == 403
    await db_session.execute(
        update(WorkspaceMember).where(WorkspaceMember.id == membership.id).values(role="owner")
    )
    await db_session.commit()
    response = await auth_client.get(endpoint + "?include_native_inputs=true")
    assert response.status_code == 200, response.text
    archive = response.json()
    assert verify_archive(archive)["native_recompute_inputs"]
    assert archive["policy"]["credentials"] == "excluded"
    native = archive["sections"]["native_aggregates"][0]
    key = ("fixture.driver", "1.0", "sha256:" + "a" * 64, "dataset-digest")
    assert (
        recompute_archive(archive, {key: lambda payload: deepcopy(native["result"])})[0]["status"]
        == "recomputed"
    )


def test_private_behavior_fingerprint_excludes_keys_but_binds_routes_and_runtime():
    from app.benchmarks.native_aggregation import behavior_digest

    payload = {
        "benchmark_manifest": {"id": "fixture", "version": "1", "image": "pinned"},
        "harness_manifest": {"id": "pi", "version": "1", "image": "pinned"},
        "harness_spec": {"model": {"name": "model", "endpoint": "http://model.test"}},
        "benchmark_spec": {"models": {}},
        "auxiliary_models": {},
        "harness_credentials": {"model_api_key": "synthetic-key-one"},
    }
    original = behavior_digest(payload)
    payload["harness_credentials"]["model_api_key"] = "synthetic-key-two"
    assert behavior_digest(payload) == original
    payload["harness_spec"]["model"]["endpoint"] = "http://other-model.test"
    assert behavior_digest(payload) != original


@pytest.mark.parametrize("driver", [{"snapshot": SNAPSHOT}], indirect=True)
@pytest.mark.parametrize("mixed_model", [False, True])
async def test_matrix_population_keeps_missing_cases_and_rejects_mixed_conditions(
    driver, db_session, mixed_model
):
    import uuid
    from datetime import UTC, datetime, timedelta

    from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
    from app.models.benchmark_trial import BenchmarkTrial
    from app.models.experiment import Experiment, ExperimentRun
    from app.models.task import Task

    controller, spec = driver
    experiment = Experiment(
        workspace_id=controller.owner.workspace_id,
        name="Native population",
        dataset={"source": "benchmark_sdk"},
        dataset_cases=[{"case_key": key} for key in ("one", "two", "missing")],
        configurations=[{"config_key": "cfg-01", "fingerprint": "a" * 64}],
        matrix_spec={},
    )
    db_session.add(experiment)
    await db_session.flush()
    current_task = Task(
        workspace_id=controller.owner.workspace_id,
        title="Current native member",
        run_config={
            "experiment": {"id": str(experiment.id), "config_key": "cfg-01", "run_index": 0}
        },
    )
    previous_task = Task(workspace_id=controller.owner.workspace_id, title="Prior native member")
    db_session.add_all([current_task, previous_task])
    await db_session.flush()
    source = await db_session.get(BenchmarkTrial, controller.owner.trial_id)
    source.task_id = current_task.id
    previous_snapshot = deepcopy(SNAPSHOT)
    previous_snapshot["selection"]["case_id"] = "two"
    previous_snapshot["dataset"]["case"]["case_id"] = "two"
    if mixed_model:
        previous_snapshot["harness"]["model"] = "different-model"
    previous = BenchmarkTrial(
        workspace_id=controller.owner.workspace_id,
        task_id=previous_task.id,
        snapshot=previous_snapshot,
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
        phase="finished",
        execution_status="success",
        evaluation_status="completed",
        recording_status="complete",
        cleanup_status="complete",
        finished_at=datetime.now(UTC),
    )
    db_session.add(previous)
    await db_session.flush()
    identifier = uuid.uuid4()
    result = {
        "status": "completed",
        "success": True,
        "metrics": [],
        "native": {},
        "artifacts": [],
        "completeness": {},
    }
    db_session.add(
        BenchmarkEvaluationAttempt(
            id=identifier,
            trial_id=previous.id,
            workspace_id=previous.workspace_id,
            kind="initial",
            origin_generation=1,
            controller_id=uuid.uuid4(),
            worker_actor_id=uuid.uuid4(),
            input_digest="b" * 64,
            private_inputs=b"fixture-inputs",
            status="completed",
            verdict="correct",
            success=True,
            metrics=[],
            private_result=controller.store.private.seal(
                "evaluation-result:" + str(identifier), result
            ),
            result_digest=identity(result),
            finished_at=datetime.now(UTC),
        )
    )
    for key, task_id in (("one", current_task.id), ("two", previous_task.id), ("missing", None)):
        db_session.add(
            ExperimentRun(
                experiment_id=experiment.id,
                config_key="cfg-01",
                case_key=key,
                run_index=0,
                task_id=task_id,
                status="pending" if task_id is None else "running",
            )
        )
    await db_session.commit()
    controller.aggregation = NativeAggregation(
        controller.store, controller.owner, controller.host.manifest
    )
    outcome = await controller.run(spec)
    assert outcome["evaluation"] == "completed"
    row, value = await receipt(controller)
    if mixed_model:
        assert row.event == "aggregate.unavailable" and value["reason"] == "POPULATION_CONFLICT"
    else:
        assert value["result"]["expected"] == 3 and value["result"]["observed"] == 2
        assert value["origin"]["run_index"] == 0 and len(value["members"]) == 2
