"""Native matrix lifecycle over the same immutable trial and launch queue as Work."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from evalhive_benchmark import ProtocolError
from evalhive_benchmark.peer import identity
from sqlalchemy import select

from app.benchmarks.experiment_inputs import prepare
from app.benchmarks.experiment_plans import freeze_plan, load_plan
from app.benchmarks.launch import enqueue_frozen_execution
from app.benchmarks.usage import trial_usage
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_experiment_plan import BenchmarkExperimentPlan
from app.models.benchmark_job import BenchmarkJob
from app.models.benchmark_trial import BenchmarkTrial
from app.models.experiment import Experiment, ExperimentRun
from app.models.task import Task
from app.models.workspace import Workspace


def native(experiment):
    return (experiment.dataset or {}).get("source") == "benchmark_sdk"


def store_for_product():
    from app.benchmarks.dispatch import product_store

    return product_store()


async def lock(db, experiment):
    return await db.scalar(
        select(Experiment)
        .where(
            Experiment.id == experiment.id,
            Experiment.workspace_id == experiment.workspace_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def create(db, workspace_id, payload, *, created_by="user", lineage=None):
    from app.quality.experiments import experiment_input_fingerprint

    prepared = await prepare(db, workspace_id, payload)
    store = store_for_product()
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("Experiment name is required")
    experiment = Experiment(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=name,
        description=payload.get("description"),
        dataset=prepared.source,
        dataset_cases=prepared.cases,
        configurations=prepared.configurations,
        matrix_spec={
            "configurations": payload.get("configurations") or [],
            "axes": payload.get("axes"),
        },
        n_runs_per_cell=prepared.repeats,
        max_parallel=1,
        eval_config={"mode": "native"},
        created_by=created_by,
        cloned_from_id=lineage[0] if lineage else None,
        cloned_from_revision=lineage[1] if lineage else None,
        clone_mode=lineage[2] if lineage else None,
    )
    db.add(experiment)
    await db.flush()
    conditions = {}
    for config_key, case_key, frozen in prepared.plans:
        row = await freeze_plan(db, store, experiment, config_key, case_key, frozen)
        conditions.setdefault(config_key, {})[case_key] = row.condition_digest
    experiment.configurations = [
        {
            **config,
            "fingerprint": identity(
                {"variation": config["fingerprint"], "conditions": conditions[config["config_key"]]}
            ),
            "resolved": {"native_conditions": conditions[config["config_key"]]},
        }
        for config in prepared.configurations
    ]
    experiment.input_fingerprint = experiment_input_fingerprint(experiment)
    await db.commit()
    await db.refresh(experiment)
    return experiment


async def start(db, experiment):
    experiment = await lock(db, experiment)
    if experiment.status != "draft":
        raise ValueError("Only a draft native matrix can start")
    configs = [row for row in experiment.configurations if not row.get("retired_at")]
    keys = {config["config_key"] for config in configs}
    actual = set(
        await db.execute(
            select(BenchmarkExperimentPlan.config_key, BenchmarkExperimentPlan.case_key).where(
                BenchmarkExperimentPlan.experiment_id == experiment.id,
                BenchmarkExperimentPlan.workspace_id == experiment.workspace_id,
                BenchmarkExperimentPlan.config_key.in_(keys),
            )
        )
    )
    expected = {
        (config["config_key"], case["case_key"])
        for config in configs
        for case in experiment.dataset_cases
    }
    if not expected or actual != expected:
        raise ValueError("Frozen native matrix inputs are incomplete")
    for config in configs:
        for case in experiment.dataset_cases:
            for repeat in range(experiment.n_runs_per_cell):
                db.add(
                    ExperimentRun(
                        experiment_id=experiment.id,
                        config_key=config["config_key"],
                        case_key=case["case_key"],
                        run_index=repeat,
                    )
                )
    experiment.status, experiment.started_at = "running", datetime.now(UTC).replace(tzinfo=None)
    await db.commit()


async def cancel(db, experiment):
    experiment = await lock(db, experiment)
    if experiment.status not in {"draft", "running", "paused"}:
        raise ValueError("Native matrix is already terminal")
    rows = list(
        await db.scalars(
            select(ExperimentRun).where(
                ExperimentRun.experiment_id == experiment.id, ExperimentRun.retired_at.is_(None)
            )
        )
    )
    active = False
    for row in rows:
        if row.status == "pending":
            row.status, row.completed_at = "skipped", datetime.now(UTC).replace(tzinfo=None)
        elif row.status in {"running", "evaluating"} and row.task_id is not None:
            trial = await db.scalar(
                select(BenchmarkTrial).where(
                    BenchmarkTrial.workspace_id == experiment.workspace_id,
                    BenchmarkTrial.task_id == row.task_id,
                )
            )
            if trial is not None:
                await store_for_product().request_cancel(experiment.workspace_id, trial.id)
                active = True
    experiment.status = "cancelled"
    if not active:
        experiment.completed_at = datetime.now(UTC).replace(tzinfo=None)
    await db.commit()


async def settle(db, store, experiment, row):
    trial = await db.scalar(
        select(BenchmarkTrial)
        .execution_options(populate_existing=True)
        .where(
            BenchmarkTrial.workspace_id == experiment.workspace_id,
            BenchmarkTrial.task_id == row.task_id,
        )
    )
    if trial is None:
        row.status, row.failure_type = "failed", "infra"
        row.completed_at = datetime.now(UTC).replace(tzinfo=None)
        return
    if trial.phase != "finished":
        row.status = "evaluating" if trial.phase == "evaluating" else "running"
        return
    job = await db.scalar(
        select(BenchmarkJob)
        .execution_options(populate_existing=True)
        .where(
            BenchmarkJob.trial_id == trial.id,
            BenchmarkJob.workspace_id == experiment.workspace_id,
            BenchmarkJob.kind == "execution",
        )
    )
    if job is not None and job.status in {"queued", "running"}:
        return
    evaluation = await db.scalar(
        select(BenchmarkEvaluationAttempt).where(
            BenchmarkEvaluationAttempt.workspace_id == experiment.workspace_id,
            BenchmarkEvaluationAttempt.trial_id == trial.id,
            BenchmarkEvaluationAttempt.kind == "initial",
        )
    )
    completed = (
        trial.execution_status == "success"
        and trial.evaluation_status in {"completed", "unavailable"}
        and trial.recording_status == "complete"
        and trial.cleanup_status == "complete"
    )
    row.status = "success" if completed else "failed"
    row.external_verdict = (
        evaluation.success if evaluation is not None and evaluation.status == "completed" else None
    )
    row.weighted_score = row.trajectory_score = None
    task = await db.get(Task, row.task_id, populate_existing=True)
    row.failure_type = task.failure_type if task is not None else "infra"
    if not completed and trial.execution_status == "success":
        row.failure_type = "infra"
    row.duration_seconds = max(0, int((trial.finished_at - trial.created_at).total_seconds()))
    row.completed_at = trial.finished_at.astimezone(UTC).replace(tzinfo=None)
    usage = await trial_usage(store, experiment.workspace_id, trial.id)
    groups = usage["groups"]
    row.cost_usd = (
        sum((Decimal(group["cost"]["amount"]) for group in groups), Decimal(0))
        if groups
        and usage["valid"]
        and all(group["cost"]["amount"] is not None for group in groups)
        else None
    )


async def advance(db, experiment):
    experiment = await lock(db, experiment)
    if experiment.status not in {"running", "paused", "cancelled"}:
        return
    store = store_for_product()
    rows = list(
        await db.scalars(
            select(ExperimentRun)
            .where(
                ExperimentRun.experiment_id == experiment.id,
                ExperimentRun.retired_at.is_(None),
            )
            .order_by(ExperimentRun.run_index, ExperimentRun.case_key, ExperimentRun.config_key)
            .with_for_update()
        )
    )
    for row in rows:
        if row.status in {"running", "evaluating"}:
            await settle(db, store, experiment, row)
    experiment.accumulated_cost_usd = sum(
        (row.cost_usd for row in rows if row.cost_usd is not None), Decimal(0)
    )
    pending = [row for row in rows if row.status == "pending"]
    active = [row for row in rows if row.status in {"running", "evaluating"}]
    if experiment.status == "running" and pending and not active:
        busy = await db.scalar(
            select(BenchmarkJob.id).where(BenchmarkJob.status.in_(("queued", "running"))).limit(1)
        )
        if busy is None:
            row = pending[0]
            trial_id, task_id = uuid.uuid4(), uuid.uuid4()
            try:
                frozen = await load_plan(
                    db,
                    store,
                    experiment.workspace_id,
                    experiment.id,
                    row.config_key,
                    row.case_key,
                    trial_id=trial_id,
                    task_id=task_id,
                )
                workspace = await db.get(Workspace, experiment.workspace_id)
                document = {
                    "kind": "execution",
                    "experiment_id": str(experiment.id),
                    "cell_id": str(row.id),
                    "attempt": (row.attempt_count or 0) + 1,
                }
                from app.benchmarks.matrix_mutations import previous_execution

                retry_of_id, replay_of_task_id = await previous_execution(db, experiment, row)
                job, trial = await enqueue_frozen_execution(
                    db,
                    store,
                    workspace,
                    uuid.uuid5(row.id, str(document["attempt"])),
                    document,
                    frozen,
                    trial_id=trial_id,
                    task_id=task_id,
                    origin="experiment",
                    retry_of_id=retry_of_id,
                    replay_of_task_id=replay_of_task_id,
                )
                task = await db.get(Task, trial.task_id)
                task.benchmark_suite, task.benchmark_case_id = (
                    "exp:" + str(experiment.id),
                    row.case_key,
                )
                task.run_config = {
                    **task.run_config,
                    "experiment": {
                        "id": str(experiment.id),
                        "config_key": row.config_key,
                        "case_key": row.case_key,
                        "run_index": row.run_index,
                    },
                }
                row.task_id, row.status = task.id, "running"
                row.attempt_count = document["attempt"]
                config = next(
                    config
                    for config in experiment.configurations
                    if config["config_key"] == row.config_key
                )
                # Legacy comparison columns retain 128 bits; the immutable plan
                # and configuration retain the authoritative full SHA-256.
                row.condition_fingerprint = config["resolved"]["native_conditions"][row.case_key][
                    :32
                ]
                row.core_condition_fingerprint = config["fingerprint"][:32]
            except ProtocolError:
                raise ValueError("Frozen native matrix conditions are unavailable") from None
    elif not pending and not active and experiment.status != "paused":
        if experiment.status == "running":
            experiment.status = (
                "completed" if any(row.status == "success" for row in rows) else "failed"
            )
            if experiment.status == "failed":
                experiment.error = "No native lifecycle completed successfully"
        experiment.completed_at = datetime.now(UTC).replace(tzinfo=None)
    await db.commit()
