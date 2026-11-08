"""Explicit matrix history mutations preserve immutable native trial conditions."""

import uuid

from evalhive_benchmark import ProtocolError
from sqlalchemy import select

from app.benchmarks.experiment_plans import load_plan, require_pinned_runtime
from app.benchmarks.experiments import lock, store_for_product
from app.models.benchmark_trial import BenchmarkTrial
from app.models.experiment import ExperimentAttempt, ExperimentRun
from app.models.task import Task


async def previous_execution(db, experiment, row):
    if not row.attempt_count:
        return None, None
    previous = await db.scalar(
        select(ExperimentAttempt).where(
            ExperimentAttempt.experiment_run_id == row.id,
            ExperimentAttempt.attempt_index == row.attempt_count,
        )
    )
    if previous is None or previous.task_id is None:
        return None, None
    task = await db.get(Task, previous.task_id)
    if task is None or task.workspace_id != experiment.workspace_id:
        raise ValueError("Previous native execution is unavailable in this workspace")
    reference = (task.run_config or {}).get("benchmark_trial_id")
    trial = await db.get(BenchmarkTrial, uuid.UUID(reference)) if reference else None
    if trial is None or trial.workspace_id != experiment.workspace_id or trial.task_id != task.id:
        raise ValueError("Previous native trial lineage is unavailable")
    return trial.id, task.id


async def retry_failed(db, experiment):
    from app.quality.experiments import _bump_revision, _retire_task_tag, _snapshot_attempt

    experiment = await lock(db, experiment)
    if experiment.status not in {"completed", "failed", "capped", "cancelled", "paused"}:
        raise ValueError("Pause or settle a native matrix before retrying its failed cells")
    rows = list(
        await db.scalars(
            select(ExperimentRun)
            .where(
                ExperimentRun.experiment_id == experiment.id,
                ExperimentRun.retired_at.is_(None),
                ExperimentRun.status == "failed",
            )
            .with_for_update()
        )
    )
    for row in rows:
        try:
            frozen = await load_plan(
                db,
                store_for_product(),
                experiment.workspace_id,
                experiment.id,
                row.config_key,
                row.case_key,
                trial_id=uuid.uuid4(),
                task_id=uuid.uuid4(),
            )
            require_pinned_runtime(frozen.private)
        except ProtocolError as error:
            raise ValueError(str(error)) from None
        if row.task_id is not None:
            task = await db.get(Task, row.task_id)
            reference = (task.run_config or {}).get("benchmark_trial_id") if task else None
            trial = await db.get(BenchmarkTrial, uuid.UUID(reference)) if reference else None
            if (
                trial is None
                or trial.workspace_id != experiment.workspace_id
                or trial.task_id != row.task_id
            ):
                raise ValueError("Failed native cell has no authenticated trial lineage")
            if trial.phase != "finished" or trial.cleanup_status != "complete":
                raise ValueError("Complete cleanup before retrying a native cell")
        await _snapshot_attempt(db, row, "retry")
        await _retire_task_tag(db, row.task_id)
        row.task_id, row.status = None, "pending"
        row.cost_usd = row.weighted_score = row.trajectory_score = None
        row.duration_seconds = row.failure_type = row.external_verdict = row.completed_at = None
    if rows:
        experiment.status, experiment.completed_at, experiment.error = "running", None, None
        await _bump_revision(db, experiment, "retry_failed", {"cells_retried": len(rows)})
    await db.commit()
    return len(rows)
