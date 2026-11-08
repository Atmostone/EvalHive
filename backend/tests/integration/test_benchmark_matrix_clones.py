"""Native clones preserve frozen ancestry or explicitly record changed conditions."""

import json
import uuid
from copy import deepcopy

import pytest
import pytest_asyncio
from app.api.benchmark_trials import get_trial_store
from app.benchmarks.experiment_plans import load_plan
from app.config import get_settings
from app.harnesses.catalog import get_manifest
from app.models.benchmark_experiment_plan import BenchmarkExperimentPlan
from app.models.experiment import Experiment, ExperimentRun
from app.models.task import Task
from app.quality import experiments as service
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from tests.integration.test_benchmark_experiments import payload
from tests.integration.test_benchmark_launch_inputs import input_selection as input_selection
from tests.unit.test_benchmark_installations import registry as registry


@pytest_asyncio.fixture
async def source_matrix(auth_client, input_selection, db_session, monkeypatch, tmp_path):
    _, selection, _, _, _ = input_selection
    declaration = get_manifest("pi")
    declaration["requirements"]["runtime_bundle"]["image"] = "sha256:" + "3" * 64
    root = tmp_path / "harnesses" / "pi"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps(declaration))
    monkeypatch.setattr(get_settings(), "harness_manifest_dir", str(root.parent))
    request = {
        **payload(selection),
        "n_runs_per_cell": 1,
        "configurations": [
            {
                "harness_profile_id": selection["harness_profile_id"],
                "harness_config": {"temperature": temperature},
            }
            for temperature in (0, 0.3)
        ],
    }
    response = await auth_client.post("/api/experiments", json=request)
    assert response.status_code == 201, response.text
    return await db_session.get(Experiment, uuid.UUID(response.json()["id"]))


async def test_frozen_clone_keeps_plans_after_profile_edits_and_does_not_restore_retired_config(
    auth_client,
    db_session,
    source_matrix,
    input_selection,
):
    source = source_matrix
    retired = await auth_client.delete(f"/api/experiments/{source.id}/configs/cfg-02")
    assert retired.status_code == 200, retired.text
    await db_session.refresh(source)
    assert source.revision == 2
    original = await db_session.scalar(
        select(BenchmarkExperimentPlan)
        .options(undefer(BenchmarkExperimentPlan.private_inputs))
        .where(
            BenchmarkExperimentPlan.experiment_id == source.id,
            BenchmarkExperimentPlan.config_key == "cfg-01",
        )
    )
    profile = input_selection[2]
    profile.config = {"steps": 900}
    await db_session.commit()
    response = await auth_client.post(
        f"/api/experiments/{source.id}/clone",
        json={"name": "Recorded native clone", "changes": {"n_runs_per_cell": 3}},
    )
    assert response.status_code == 201, response.text
    public = response.json()
    assert public["cloned_from_id"] == str(source.id) and public["cloned_from_revision"] == 2
    assert public["clone_mode"] == "frozen" and public["n_configs"] == 1
    cloned = await db_session.get(Experiment, uuid.UUID(public["id"]))
    copied = await db_session.scalar(
        select(BenchmarkExperimentPlan)
        .options(undefer(BenchmarkExperimentPlan.private_inputs))
        .where(BenchmarkExperimentPlan.experiment_id == cloned.id)
    )
    assert (
        copied.source_plan_id == original.id
        and copied.condition_digest == original.condition_digest
    )
    assert copied.snapshot == original.snapshot and copied.private_inputs != original.private_inputs
    frozen = await load_plan(
        db_session,
        get_trial_store(),
        source.workspace_id,
        cloned.id,
        "cfg-01",
        source.dataset_cases[0]["case_key"],
        trial_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
    )
    assert frozen.snapshot["benchmark"]["config"] == {}
    assert await db_session.scalar(select(func.count()).select_from(Task)) == 0
    await service.start_experiment(db_session, source)
    await service.start_experiment(db_session, cloned)
    assert await db_session.scalar(select(func.count()).select_from(ExperimentRun)) == 4
    blocked = await auth_client.delete(f"/api/experiments/{source.id}")
    assert blocked.status_code == 409


async def test_explicit_new_conditions_have_distinct_plans_and_declared_ancestry(
    auth_client, db_session, source_matrix, input_selection
):
    source = source_matrix
    selection = input_selection[1]
    original = deepcopy(source.configurations)
    response = await auth_client.post(
        f"/api/experiments/{source.id}/clone",
        json={
            "changes": {
                "configurations": [
                    {
                        "harness_profile_id": selection["harness_profile_id"],
                        "harness_config": {"temperature": 0.7},
                    }
                ]
            }
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["clone_mode"] == "new_conditions"
    clone_id = uuid.UUID(response.json()["id"])
    copied = await db_session.scalar(
        select(BenchmarkExperimentPlan).where(BenchmarkExperimentPlan.experiment_id == clone_id)
    )
    assert copied.source_plan_id is None and copied.snapshot["benchmark"]["config"] == {}
    assert copied.snapshot["harness"]["config"]["temperature"] == 0.7
    await db_session.refresh(source)
    assert source.configurations == original and source.revision == 1


async def test_clone_ancestry_is_immutable_and_source_cannot_be_orphaned(
    auth_client, source_matrix, _engine
):
    source = source_matrix
    response = await auth_client.post(f"/api/experiments/{source.id}/clone", json={})
    assert response.status_code == 201, response.text
    clone_id = uuid.UUID(response.json()["id"])
    async with AsyncSession(_engine) as db:
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(
                update(Experiment).where(Experiment.id == clone_id).values(cloned_from_revision=99)
            )
    async with AsyncSession(_engine) as db:
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(
                update(BenchmarkExperimentPlan)
                .where(BenchmarkExperimentPlan.experiment_id == clone_id)
                .values(source_plan_id=None)
            )
    blocked = await auth_client.delete(f"/api/experiments/{source.id}")
    assert blocked.status_code == 409, blocked.text
    assert (await auth_client.delete(f"/api/experiments/{clone_id}")).status_code == 204
    assert (await auth_client.delete(f"/api/experiments/{source.id}")).status_code == 204


async def test_clone_scope_and_origin_completeness_are_enforced(
    auth_client, source_matrix, _engine
):
    source = source_matrix
    async with AsyncSession(_engine) as db:
        db.add(
            Experiment(
                workspace_id=source.workspace_id,
                name="Incomplete clone origin",
                dataset={},
                dataset_cases=[],
                configurations=[],
                matrix_spec={},
                cloned_from_id=source.id,
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
    for changes in ({"n_toolathlon_lanes": 1}, {"n_runs_per_cell": 100}, {"max_parallel": 2}):
        response = await auth_client.post(
            f"/api/experiments/{source.id}/clone", json={"changes": changes}
        )
        assert response.status_code == 400, response.text


async def test_extend_draft_preserves_recorded_plans_and_never_reuses_retired_keys(
    auth_client, source_matrix, db_session, input_selection
):
    source = source_matrix
    original = list(
        await db_session.scalars(
            select(BenchmarkExperimentPlan).where(
                BenchmarkExperimentPlan.experiment_id == source.id
            )
        )
    )
    before = [(row.id, row.condition_digest, deepcopy(row.snapshot)) for row in original]
    assert (
        await auth_client.delete(f"/api/experiments/{source.id}/configs/cfg-02")
    ).status_code == 200
    response = await auth_client.post(
        f"/api/experiments/{source.id}/add-config",
        json={
            "harness_profile_id": input_selection[1]["harness_profile_id"],
            "harness_config": {"temperature": 0.8},
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["config_key"] == "cfg-03" and response.json()["runs_created"] == 0
    assert response.json()["n_configs"] == 2 and response.json()["status"] == "draft"
    for row, values in zip(original, before, strict=True):
        await db_session.refresh(row)
        assert (row.id, row.condition_digest, row.snapshot) == values
    await db_session.refresh(source)
    await service.start_experiment(db_session, source)
    rows = list(await db_session.scalars(select(ExperimentRun)))
    assert {row.config_key for row in rows} == {"cfg-01", "cfg-03"}


async def test_duplicate_and_unsupported_extensions_roll_back_without_partial_plans(
    auth_client, source_matrix, db_session, input_selection
):
    source = source_matrix
    for extra in ({"harness_config": {"temperature": 0}}, {"orchestrator": True}):
        response = await auth_client.post(
            f"/api/experiments/{source.id}/add-config",
            json={"harness_profile_id": input_selection[1]["harness_profile_id"], **extra},
        )
        assert response.status_code == 409, response.text
        assert (
            await db_session.scalar(select(func.count()).select_from(BenchmarkExperimentPlan)) == 2
        )
        await db_session.refresh(source)
        assert source.revision == 1 and len(source.configurations) == 2


async def test_paused_extension_creates_only_new_cells_and_waits_for_resume(
    auth_client, source_matrix, db_session, input_selection
):
    source = source_matrix
    await service.start_experiment(db_session, source)
    blocked = await auth_client.post(
        f"/api/experiments/{source.id}/add-config",
        json={"harness_profile_id": input_selection[1]["harness_profile_id"]},
    )
    assert blocked.status_code == 409
    await service.pause_experiment(db_session, source)
    response = await auth_client.post(
        f"/api/experiments/{source.id}/add-config",
        json={
            "harness_profile_id": input_selection[1]["harness_profile_id"],
            "harness_config": {"temperature": 0.9},
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "paused" and response.json()["runs_created"] == 1
    await db_session.refresh(source)
    await service.advance_experiment(db_session, source)
    assert await db_session.scalar(select(func.count()).select_from(Task)) == 0
    rows = list(await db_session.scalars(select(ExperimentRun)))
    assert len(rows) == 3 and all(row.status == "pending" for row in rows)


async def test_native_matrix_archive_retains_plans_and_ancestry_without_ordinary_report(
    auth_client, source_matrix, db_session, monkeypatch
):
    from app.api import experiments as api
    from evalhive_benchmark.archive import verify_archive

    def no_ordinary(*args, **kwargs):
        pytest.fail("Native archives cannot use ordinary report mathematics")

    monkeypatch.setattr(api, "build_bundle", no_ordinary)
    monkeypatch.setattr(api, "compute_report", no_ordinary)
    assert (await auth_client.get(f"/api/experiments/{source_matrix.id}/report")).status_code == 409
    response = await auth_client.post(f"/api/experiments/{source_matrix.id}/clone", json={})
    assert response.status_code == 201
    identifier = response.json()["id"]
    exported = await auth_client.get(
        f"/api/experiments/{identifier}/bundle?include_native_inputs=true"
    )
    assert exported.status_code == 200, exported.text
    document = exported.json()
    assert verify_archive(document)["valid"]
    matrix = document["sections"]["matrix"]
    assert matrix["cloned_from_id"] == str(source_matrix.id) and matrix["clone_mode"] == "frozen"
    assert len(document["sections"]["plans"]) == 2
    assert all(
        plan["source_plan_id"] and len(plan["condition_digest"]) == 64
        for plan in document["sections"]["plans"]
    )
    assert document["sections"]["trials"] == []
    for secret in (
        "synthetic-provider-secret",
        "synthetic-service-secret",
        "hidden-fixture-answer",
    ):
        assert secret not in exported.text
