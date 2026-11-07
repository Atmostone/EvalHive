"""Matrix input plans preserve conditions and tenant scope across fresh trials."""

import json
import uuid
from copy import deepcopy

import pytest
import pytest_asyncio
from app.api.benchmark_trials import get_trial_store
from app.benchmarks.experiment_plans import condition_digest, freeze_plan, load_plan
from app.benchmarks.launch import enqueue_frozen_execution
from app.benchmarks.launch_inputs import FrozenInputs, resolve_inputs
from app.models.benchmark_experiment_plan import BenchmarkExperimentPlan
from app.models.benchmark_job import BenchmarkJob
from app.models.benchmark_trial import BenchmarkTrial
from app.models.experiment import Experiment
from app.models.task import Task
from evalhive_benchmark import ProtocolError
from sqlalchemy import func, inspect, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.test_benchmark_launch_inputs import LIMITS
from tests.integration.test_benchmark_launch_inputs import input_selection as input_selection
from tests.unit.test_benchmark_installations import registry as registry


@pytest_asyncio.fixture
async def plan(db_session, input_selection):
    workspace, selection, _, _, _ = input_selection
    frozen = await resolve_inputs(
        db_session, workspace, selection, trial_id=uuid.uuid4(), task_id=uuid.uuid4(), limits=LIMITS
    )
    experiment = Experiment(
        workspace_id=workspace.id,
        name="Frozen native plan",
        status="draft",
        dataset={"source": "benchmark_sdk"},
        dataset_cases=[],
        configurations=[],
        matrix_spec={},
    )
    db_session.add(experiment)
    await db_session.flush()
    store = get_trial_store()
    row = await freeze_plan(db_session, store, experiment, "cfg-01", "case-01", frozen)
    await db_session.commit()
    return store, experiment, row, frozen


async def test_repeated_cells_bind_fresh_identities_without_changing_frozen_inputs(
    db_session, plan, input_selection
):
    store, experiment, row, frozen = plan
    _, _, profile, _, _ = input_selection
    original_ciphertext = row.private_inputs
    profile.config = {"steps": 500}
    await db_session.commit()
    identities = []
    for _ in range(2):
        trial_id, task_id = uuid.uuid4(), uuid.uuid4()
        loaded = await load_plan(
            db_session,
            store,
            experiment.workspace_id,
            experiment.id,
            "cfg-01",
            "case-01",
            trial_id=trial_id,
            task_id=task_id,
        )
        spec, harness = loaded.private["benchmark_spec"], loaded.private["harness_spec"]
        assert spec["scope"]["trial_id"] == str(trial_id) and spec["scope"]["generation"] == 1
        assert harness["task_id"] == str(task_id) and harness["metadata"][
            "benchmark_trial_id"
        ] == str(trial_id)
        assert loaded.snapshot == frozen.snapshot
        assert spec["config"] == frozen.private["benchmark_spec"]["config"]
        assert spec["case"]["metadata"]["gold"] == "hidden-fixture-answer"
        assert condition_digest(loaded) == row.condition_digest
        identities.append(harness["attempt_id"])
    assert identities[0] != identities[1]
    await db_session.refresh(row, attribute_names=["private_inputs"])
    assert row.private_inputs == original_ciphertext
    async with AsyncSession(store.engine) as db:
        restored = await db.get(BenchmarkExperimentPlan, row.id)
        assert "private_inputs" in inspect(restored).unloaded
        public = json.dumps(restored.snapshot)
        for hidden in (
            "hidden-fixture-answer",
            "synthetic-provider-secret",
            "synthetic-service-secret",
        ):
            assert hidden not in public and hidden.encode() not in original_ciphertext


@pytest.mark.parametrize(
    "change",
    [
        {"snapshot": {"changed": True}},
        {"private_inputs": b"changed"},
        {"condition_digest": "b" * 64},
        {"case_key": "other-case"},
    ],
)
async def test_native_matrix_plans_are_database_immutable(plan, change):
    store, _, row, _ = plan
    async with AsyncSession(store.engine) as db:
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(
                update(BenchmarkExperimentPlan)
                .where(BenchmarkExperimentPlan.id == row.id)
                .values(**change)
            )


async def test_matrix_plan_scope_cannot_be_rebound_or_read_from_another_workspace(db_session, plan):
    store, experiment, _, frozen = plan
    with pytest.raises(ProtocolError) as error:
        await load_plan(
            db_session,
            store,
            uuid.uuid4(),
            experiment.id,
            "cfg-01",
            "case-01",
            trial_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
        )
    assert error.value.code == "PLAN_UNAVAILABLE"
    changed = deepcopy(frozen.private)
    changed["benchmark_spec"]["scope"]["workspace_id"] = str(uuid.uuid4())
    with pytest.raises(ProtocolError) as error:
        await freeze_plan(
            db_session,
            store,
            experiment,
            "cfg-02",
            "case-01",
            FrozenInputs(
                frozen.snapshot,
                changed,
                frozen.title,
                frozen.description,
                frozen.harness_profile_id,
            ),
        )
    assert error.value.code == "INVALID_SCOPE"


async def test_condition_identity_excludes_rotating_keys_but_includes_model_routes(plan):
    _, _, _, frozen = plan
    changed = deepcopy(frozen.private)
    changed["harness_credentials"]["model_api_key"] = "rotated-fixture-key"
    changed["auxiliary_models"]["user"]["api_key"] = "rotated-user-key"
    rotated = FrozenInputs(
        frozen.snapshot, changed, frozen.title, frozen.description, frozen.harness_profile_id
    )
    assert condition_digest(rotated) == condition_digest(frozen)
    changed["harness_spec"]["model"]["endpoint"] = "http://127.0.0.1:10/v1"
    assert condition_digest(rotated) != condition_digest(frozen)


async def test_frozen_matrix_cell_uses_the_same_atomic_execution_queue(
    db_session, plan, input_selection
):
    store, experiment, row, _ = plan
    workspace, _, _, _, _ = input_selection
    request_id = uuid.uuid4()
    document = {
        "kind": "execution",
        "experiment_id": str(experiment.id),
        "config_key": "cfg-01",
        "case_key": "case-01",
        "repeat": 0,
        "condition_digest": row.condition_digest,
    }
    saved_ids = []
    for _ in range(2):
        trial_id, task_id = uuid.uuid4(), uuid.uuid4()
        frozen = await load_plan(
            db_session,
            store,
            workspace.id,
            experiment.id,
            "cfg-01",
            "case-01",
            trial_id=trial_id,
            task_id=task_id,
        )
        job, trial = await enqueue_frozen_execution(
            db_session,
            store,
            workspace,
            request_id,
            document,
            frozen,
            trial_id=trial_id,
            task_id=task_id,
            origin="experiment",
        )
        await db_session.commit()
        saved_ids.append((job.id, trial.id, trial.task_id))
    assert saved_ids[0] == saved_ids[1]
    task = await db_session.get(Task, saved_ids[0][2])
    assert task.origin == "experiment" and task.max_retries == 0
    assert task.run_config["benchmark_trial_id"] == str(saved_ids[0][1])
    for model in (Task, BenchmarkTrial, BenchmarkJob):
        assert await db_session.scalar(select(func.count()).select_from(model)) == 1
    assert row.private_inputs != b""


async def test_plan_migration_preserves_existing_experiment_data(auth_client, db_session):
    import asyncio

    from alembic import command

    from tests.integration.test_migrations import _alembic_cfg

    original = {"source": "upload", "n_cases": 1}
    experiment = Experiment(
        workspace_id=uuid.UUID(auth_client.headers["X-Workspace-Id"]),
        name="Historical matrix",
        dataset=original,
        dataset_cases=[{"case_key": "historical-case", "title": "Original"}],
        matrix_spec={},
        configurations=[],
    )
    db_session.add(experiment)
    await db_session.commit()
    identity_id = experiment.id
    cfg = _alembic_cfg()
    await asyncio.to_thread(command.downgrade, cfg, "bg13")
    await asyncio.to_thread(command.upgrade, cfg, "head")
    await db_session.refresh(experiment)
    assert experiment.id == identity_id and experiment.dataset == original
    assert experiment.dataset_cases == [{"case_key": "historical-case", "title": "Original"}]
