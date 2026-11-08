"""Export native records without configuration secrets or implicit model calls."""

import uuid

from evalhive_benchmark import ProtocolError
from evalhive_benchmark.archive import MAX_ARCHIVE_BYTES, canonical, create_archive
from sqlalchemy import select

from app.benchmarks.aggregate_read import read_records
from app.benchmarks.usage import trial_usage
from app.models.benchmark_artifact import BenchmarkArtifact
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_evaluation_control import BenchmarkEvaluationControl
from app.models.benchmark_experiment_plan import BenchmarkExperimentPlan
from app.models.benchmark_trial import BenchmarkTrial
from app.models.experiment import Experiment, ExperimentAttempt, ExperimentRun
from app.models.task import Task

MAX_RECORDS = 1000


def evaluation(row):
    return {
        "id": str(row.id),
        "trial_id": str(row.trial_id),
        "kind": row.kind,
        "source_evaluation_id": str(row.source_evaluation_id) if row.source_evaluation_id else None,
        "input_digest": row.input_digest,
        "result_digest": row.result_digest,
        "status": row.status,
        "success": row.success,
        "verdict": row.verdict,
        "metrics": row.metrics,
        "primary_metric": row.primary_metric,
        "error_kind": row.error_kind,
    }


async def trial_archive(db, store, workspace_id, trial_id, *, include_native_inputs=False):
    trial = await db.scalar(
        select(BenchmarkTrial).where(
            BenchmarkTrial.id == trial_id, BenchmarkTrial.workspace_id == workspace_id
        )
    )
    if trial is None:
        raise ProtocolError("TRIAL_UNAVAILABLE", "Native archive is unavailable", "configuration")
    if trial.phase != "finished":
        raise ProtocolError(
            "TRIAL_ACTIVE", "Settle native execution before exporting its archive", "configuration"
        )
    active = await db.scalar(
        select(BenchmarkEvaluationControl.id)
        .where(
            BenchmarkEvaluationControl.source_trial_id == trial_id,
            BenchmarkEvaluationControl.workspace_id == workspace_id,
            BenchmarkEvaluationControl.phase != "finished",
        )
        .limit(1)
    )
    if active is not None:
        raise ProtocolError(
            "EVALUATION_ACTIVE",
            "Settle native reevaluation before exporting its archive",
            "configuration",
        )
    evaluations = list(
        await db.scalars(
            select(BenchmarkEvaluationAttempt)
            .where(
                BenchmarkEvaluationAttempt.trial_id == trial_id,
                BenchmarkEvaluationAttempt.workspace_id == workspace_id,
            )
            .order_by(BenchmarkEvaluationAttempt.started_at, BenchmarkEvaluationAttempt.id)
            .limit(MAX_RECORDS + 1)
        )
    )
    artifacts = list(
        await db.scalars(
            select(BenchmarkArtifact)
            .where(
                BenchmarkArtifact.trial_id == trial_id,
                BenchmarkArtifact.workspace_id == workspace_id,
                BenchmarkArtifact.visibility.in_(("agent", "user")),
            )
            .order_by(BenchmarkArtifact.id)
            .limit(MAX_RECORDS + 1)
        )
    )
    if max(len(evaluations), len(artifacts)) > MAX_RECORDS:
        raise ProtocolError(
            "ARCHIVE_TOO_LARGE", "Native record history exceeds the export limit", "unsupported"
        )
    aggregates, aggregate_bytes = [], 0
    for offset in range(0, MAX_RECORDS + 1, 20):
        page = await read_records(db, store, workspace_id, trial_id, offset=offset)
        for _, _, record in page:
            aggregate_bytes += len(canonical(record))
            if aggregate_bytes > MAX_ARCHIVE_BYTES // 2:
                raise ProtocolError(
                    "ARCHIVE_TOO_LARGE",
                    "Select a smaller native aggregation history",
                    "unsupported",
                )
            aggregates.append(record)
        if len(page) < 20:
            break
    if len(aggregates) > MAX_RECORDS:
        raise ProtocolError(
            "ARCHIVE_TOO_LARGE",
            "Native aggregation history exceeds the export limit",
            "unsupported",
        )
    return create_archive(
        {
            "trial": {
                "id": str(trial.id),
                "task_id": str(trial.task_id) if trial.task_id else None,
                "retry_of_id": str(trial.retry_of_id) if trial.retry_of_id else None,
                "snapshot": trial.snapshot,
                "phase": trial.phase,
                "execution_status": trial.execution_status,
                "evaluation_status": trial.evaluation_status,
                "recording_status": trial.recording_status,
                "cleanup_status": trial.cleanup_status,
                "journal_watermark": trial.next_seq - 1,
            },
            "evaluations": [evaluation(row) for row in evaluations],
            "native_aggregates": aggregates,
            "evidence": [
                {
                    "id": str(row.id),
                    "logical_id": row.logical_id,
                    "digest": row.digest,
                    "size_bytes": row.size_bytes,
                    "visibility": row.visibility,
                    "availability": "retained_outside_archive",
                }
                for row in artifacts
            ],
            "usage": await trial_usage(store, workspace_id, trial_id),
        },
        include_native_inputs=include_native_inputs,
    )


async def matrix_archive(db, store, workspace_id, experiment_id, *, include_native_inputs=False):
    experiment = await db.scalar(
        select(Experiment).where(
            Experiment.id == experiment_id, Experiment.workspace_id == workspace_id
        )
    )
    if experiment is None or (experiment.dataset or {}).get("source") != "benchmark_sdk":
        raise ProtocolError(
            "MATRIX_UNAVAILABLE", "Native matrix archive is unavailable", "configuration"
        )
    if experiment.status in {"running", "paused"} or (
        experiment.status == "cancelled" and not experiment.completed_at
    ):
        raise ProtocolError(
            "MATRIX_ACTIVE",
            "Settle the native matrix before exporting its archive",
            "configuration",
        )
    plans = list(
        await db.scalars(
            select(BenchmarkExperimentPlan)
            .where(
                BenchmarkExperimentPlan.workspace_id == workspace_id,
                BenchmarkExperimentPlan.experiment_id == experiment_id,
            )
            .order_by(BenchmarkExperimentPlan.config_key, BenchmarkExperimentPlan.case_key)
        )
    )
    rows = list(
        await db.scalars(
            select(ExperimentRun)
            .where(ExperimentRun.experiment_id == experiment_id)
            .order_by(ExperimentRun.id)
        )
    )
    attempts = list(
        await db.scalars(
            select(ExperimentAttempt)
            .join(ExperimentRun, ExperimentAttempt.experiment_run_id == ExperimentRun.id)
            .where(ExperimentRun.experiment_id == experiment_id)
            .order_by(ExperimentAttempt.id)
            .limit(MAX_RECORDS + 1)
        )
    )
    if max(len(rows), len(attempts), len(plans)) > MAX_RECORDS:
        raise ProtocolError(
            "ARCHIVE_TOO_LARGE", "Select a smaller native matrix history", "unsupported"
        )
    task_ids = {row.task_id for row in [*rows, *attempts] if row.task_id}
    trials, unavailable, trial_bytes = [], [], 0
    for task_id in sorted(task_ids):
        task = await db.get(Task, task_id)
        trial_id = (task.run_config or {}).get("benchmark_trial_id") if task else None
        if task is None or task.workspace_id != workspace_id or not trial_id:
            unavailable.append(str(task_id))
            continue
        document = await trial_archive(
            db,
            store,
            workspace_id,
            uuid.UUID(trial_id),
            include_native_inputs=include_native_inputs,
        )
        trial_bytes += len(canonical(document))
        if trial_bytes > MAX_ARCHIVE_BYTES * 3 // 4:
            raise ProtocolError(
                "ARCHIVE_TOO_LARGE", "Select a smaller native matrix history", "unsupported"
            )
        trials.append(document)
    return create_archive(
        {
            "matrix": {
                "id": str(experiment.id),
                "revision": experiment.revision,
                "name": experiment.name,
                "status": experiment.status,
                "dataset": experiment.dataset,
                "dataset_cases": experiment.dataset_cases,
                "configurations": experiment.configurations,
                "repetitions": experiment.n_runs_per_cell,
                "input_fingerprint": experiment.input_fingerprint,
                "cloned_from_id": str(experiment.cloned_from_id)
                if experiment.cloned_from_id
                else None,
                "cloned_from_revision": experiment.cloned_from_revision,
                "clone_mode": experiment.clone_mode,
            },
            "plans": [
                {
                    "id": str(plan.id),
                    "source_plan_id": str(plan.source_plan_id) if plan.source_plan_id else None,
                    "config_key": plan.config_key,
                    "case_key": plan.case_key,
                    "condition_digest": plan.condition_digest,
                    "snapshot": plan.snapshot,
                }
                for plan in plans
            ],
            "cells": [
                {
                    "id": str(row.id),
                    "config_key": row.config_key,
                    "case_key": row.case_key,
                    "run_index": row.run_index,
                    "status": row.status,
                    "retired": row.retired_at is not None,
                    "task_id": str(row.task_id) if row.task_id else None,
                    "native_correctness": row.external_verdict,
                    "cost_usd": str(row.cost_usd) if row.cost_usd is not None else None,
                    "attempt_count": row.attempt_count,
                }
                for row in rows
            ],
            "attempts": [
                {
                    "id": str(row.id),
                    "cell_id": str(row.experiment_run_id),
                    "attempt_index": row.attempt_index,
                    "task_id": str(row.task_id) if row.task_id else None,
                    "status": row.status,
                    "native_correctness": row.external_verdict,
                    "retired_reason": row.retired_reason,
                }
                for row in attempts
            ],
            "trials": trials,
            "unavailable_task_records": unavailable,
            "native_aggregates": [],
        },
        include_native_inputs=include_native_inputs,
    )
