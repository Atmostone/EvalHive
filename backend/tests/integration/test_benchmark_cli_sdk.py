"""The Case Store load command creates native SDK matrices without legacy agents."""

import json
import uuid
from argparse import Namespace

import pytest
from app.cli import benchmark as cli
from app.models.benchmark import BenchmarkDatasetRevision
from app.models.benchmark_experiment_plan import BenchmarkExperimentPlan
from app.models.benchmark_job import BenchmarkJob
from app.models.experiment import Experiment, ExperimentRun
from app.models.task import Task
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.integration.test_benchmark_launch_inputs import input_selection as input_selection
from tests.unit.test_benchmark_installations import registry as registry


def arguments(workspace, selection, suite):
    return Namespace(
        workspace_id=str(workspace.id),
        benchmark_profile=selection["profile_id"],
        dataset_revision=selection["dataset_revision_id"],
        harness_profile=selection["harness_profile_id"],
        suite=suite,
        name="Native CLI fixture",
        repeat=2,
        template=None,
        model=None,
    )


async def test_cli_load_freezes_native_matrix_without_legacy_materialization(
    input_selection, db_session, monkeypatch, capsys
):
    workspace, selection, _, _, _ = input_selection
    dataset = await db_session.get(
        BenchmarkDatasetRevision, uuid.UUID(selection["dataset_revision_id"])
    )
    monkeypatch.setattr(
        cli, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False)
    )
    await cli._load(arguments(workspace, selection, dataset.dataset_id))
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "running" and output["cases"] == 1 and output["repetitions"] == 2
    experiment = await db_session.get(Experiment, uuid.UUID(output["experiment_id"]))
    assert experiment.dataset["source"] == "benchmark_sdk"
    assert await db_session.scalar(select(func.count()).select_from(BenchmarkExperimentPlan)) == 1
    runs = list(await db_session.scalars(select(ExperimentRun)))
    assert len(runs) == 2 and all(row.status == "pending" for row in runs)
    for model in (Task, BenchmarkJob):
        assert await db_session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("template,profile", [(str(uuid.uuid4()), str(uuid.uuid4())), (None, None)])
async def test_cli_rejects_legacy_overrides_or_missing_native_selection_before_database(
    monkeypatch, template, profile
):
    def unavailable():
        pytest.fail("Rejected CLI inputs must not open a database or model connection")

    monkeypatch.setattr(cli, "async_session", unavailable)
    args = Namespace(
        template=template,
        model=None,
        benchmark_profile=profile,
        dataset_revision=profile,
        harness_profile=profile,
    )
    with pytest.raises(ValueError):
        await cli._load(args)


async def test_cli_suite_and_workspace_selection_cannot_change_the_registered_source(
    input_selection, db_session, monkeypatch
):
    workspace, selection, _, _, _ = input_selection
    monkeypatch.setattr(
        cli, "async_session", async_sessionmaker(db_session.bind, expire_on_commit=False)
    )
    with pytest.raises(ValueError, match="registered workspace dataset"):
        await cli._load(arguments(workspace, selection, "another-source"))
    assert await db_session.scalar(select(func.count()).select_from(Experiment)) == 0
