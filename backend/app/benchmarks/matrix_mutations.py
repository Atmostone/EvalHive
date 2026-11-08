"""Explicit matrix history mutations preserve immutable native trial conditions."""

import uuid
from copy import deepcopy

from evalhive_benchmark import ProtocolError
from evalhive_benchmark.peer import identity
from sqlalchemy import select

from app.benchmarks.experiment_inputs import MAX_CONFIGS, MAX_RUNS, prepare
from app.benchmarks.experiment_plans import freeze_plan, load_plan, require_pinned_runtime
from app.benchmarks.experiments import lock, store_for_product
from app.models.benchmark_trial import BenchmarkTrial
from app.models.experiment import ExperimentAttempt, ExperimentRun
from app.models.task import Task


async def add_config(db, experiment, configuration):
    from app.quality.experiments import _bump_revision

    experiment = await lock(db, experiment)
    if experiment.status not in {"draft", "paused", "completed", "failed", "cancelled", "capped"}:
        raise ValueError("Pause or settle the native matrix before adding a configuration")
    existing = experiment.configurations or []
    live = [config for config in existing if not config.get("retired_at")]
    if len(live) >= MAX_CONFIGS or (
        (len(live) + 1) * len(experiment.dataset_cases) * experiment.n_runs_per_cell > MAX_RUNS
    ):
        raise ValueError("Extended native matrix exceeds host population limits")
    source = {
        **experiment.dataset,
        "case_ids": [case["dataset_case_id"] for case in experiment.dataset_cases],
    }
    prepared = await prepare(
        db,
        experiment.workspace_id,
        {
            "dataset": source,
            "configurations": [configuration],
            "n_runs_per_cell": experiment.n_runs_per_cell,
        },
    )
    if prepared.cases != experiment.dataset_cases:
        raise ValueError("The registered dataset no longer matches the frozen matrix cases")
    number = (
        max((int(config["config_key"].removeprefix("cfg-")) for config in existing), default=0) + 1
    )
    key = f"cfg-{number:02d}"
    store = store_for_product()
    conditions = {}
    for _, case_key, frozen in prepared.plans:
        row = await freeze_plan(db, store, experiment, key, case_key, frozen, extension=True)
        conditions[case_key] = row.condition_digest
    config = {
        **prepared.configurations[0],
        "config_key": key,
        "fingerprint": identity(
            {"variation": prepared.configurations[0]["fingerprint"], "conditions": conditions}
        ),
        "resolved": {"native_conditions": conditions},
    }
    if any(item["fingerprint"] == config["fingerprint"] for item in live):
        raise ValueError("A configuration with these recorded native conditions already exists")
    experiment.configurations = [*existing, config]
    experiment.matrix_spec = {
        "configurations": [
            {
                name: deepcopy(value)
                for name, value in item.items()
                if name not in {"config_key", "fingerprint", "resolved", "retired_at"}
            }
            for item in experiment.configurations
            if not item.get("retired_at")
        ],
        "axes": None,
    }
    created = 0
    if experiment.status != "draft":
        for case in experiment.dataset_cases:
            for repeat in range(experiment.n_runs_per_cell):
                db.add(
                    ExperimentRun(
                        experiment_id=experiment.id,
                        config_key=key,
                        case_key=case["case_key"],
                        run_index=repeat,
                    )
                )
                created += 1
        if experiment.status != "paused":
            experiment.status, experiment.completed_at, experiment.error = "running", None, None
    await _bump_revision(db, experiment, "add_config", {"config_key": key, "runs_created": created})
    await db.commit()
    return {"config_key": key, "label": config["label"], "runs_created": created}


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


async def retire_config(db, experiment, config_key):
    from datetime import UTC, datetime

    from app.quality.experiments import _bump_revision, _retire_task_tag, _snapshot_attempt

    experiment = await lock(db, experiment)
    if experiment.status == "running":
        raise ValueError("Pause or settle the native matrix before retiring a configuration")
    configs = experiment.configurations or []
    target = next(
        (
            config
            for config in configs
            if config["config_key"] == config_key and not config.get("retired_at")
        ),
        None,
    )
    if target is None or sum(not config.get("retired_at") for config in configs) <= 1:
        raise ValueError("Select a live configuration and retain at least one configuration")
    rows = list(
        await db.scalars(
            select(ExperimentRun)
            .where(
                ExperimentRun.experiment_id == experiment.id,
                ExperimentRun.config_key == config_key,
            )
            .with_for_update()
        )
    )
    if any(row.status in {"running", "preprocessing", "evaluating"} for row in rows):
        raise ValueError("Finish native execution and cleanup before retiring its configuration")
    now = datetime.now(UTC).replace(tzinfo=None)
    for row in rows:
        if row.status == "pending":
            row.status, row.completed_at = "skipped", now
        await _snapshot_attempt(db, row, "config_retired")
        await _retire_task_tag(db, row.task_id)
        row.retired_at = now
    experiment.configurations = [
        {**config, "retired_at": now.isoformat()} if config["config_key"] == config_key else config
        for config in configs
    ]
    remaining = list(
        await db.scalars(
            select(ExperimentRun).where(
                ExperimentRun.experiment_id == experiment.id,
                ExperimentRun.retired_at.is_(None),
            )
        )
    )
    from decimal import Decimal

    experiment.accumulated_cost_usd = sum(
        (row.cost_usd for row in remaining if row.cost_usd is not None), Decimal(0)
    )
    if (
        experiment.status in {"completed", "failed", "capped"}
        and remaining
        and all(row.status in {"success", "failed", "skipped"} for row in remaining)
    ):
        experiment.status = (
            "completed" if any(row.status == "success" for row in remaining) else "failed"
        )
        experiment.error = (
            None
            if experiment.status == "completed"
            else "No native lifecycle completed successfully"
        )
    await _bump_revision(
        db, experiment, "retire_config", {"config_key": config_key, "cells_retired": len(rows)}
    )
    await db.commit()
    return {"retired": config_key, "cells_retired": len(rows)}
