"""Clone recorded native plans or explicitly resolve new conditions, with ancestry."""

import uuid
from copy import deepcopy

from evalhive_benchmark import ProtocolError
from sqlalchemy import select

from app.benchmarks.experiment_inputs import AXES, MAX_REPEATS, MAX_RUNS
from app.benchmarks.experiment_plans import freeze_plan, load_plan, require_pinned_runtime
from app.benchmarks.experiments import create, lock, store_for_product
from app.models.benchmark_experiment_plan import BenchmarkExperimentPlan
from app.models.experiment import Experiment


async def clone(db, source, *, name=None, changes=None, created_by="user"):
    from app.quality.experiments import experiment_input_fingerprint

    source = await lock(db, source)
    changes = dict(changes or {})
    allowed = {
        "name",
        "description",
        "dataset",
        "configurations",
        "axes",
        "n_runs_per_cell",
        "max_parallel",
        "budget_limit_usd",
    }
    if changes.keys() - allowed:
        raise ValueError("Native clone changes require supported explicit conditions")
    title = name or changes.get("name") or f"{source.name} (copy-{uuid.uuid4().hex[:4]})"
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 255:
        raise ValueError("Native clone name must contain 1 to 255 characters")
    description = changes.get("description", source.description)
    if description is not None and not isinstance(description, str):
        raise ValueError("Native clone description must be text")
    repeats = changes.get("n_runs_per_cell", source.n_runs_per_cell)
    if type(repeats) is not int or not 1 <= repeats <= MAX_REPEATS:
        raise ValueError("Native clone repetitions are outside host policy")
    if changes.get("max_parallel") not in (None, 1) or changes.get("budget_limit_usd") is not None:
        raise ValueError("Native clone requires serial execution and verifiable budgets")
    live = [deepcopy(config) for config in source.configurations if not config.get("retired_at")]
    if not live or len(live) * len(source.dataset_cases) * repeats > MAX_RUNS:
        raise ValueError("Native clone population is empty or exceeds host policy")
    if any(key in changes for key in ("dataset", "configurations", "axes")):
        configs = changes.get(
            "configurations",
            []
            if "axes" in changes
            else [
                {key: value for key, value in config.items() if key in AXES | {"label"}}
                for config in live
            ],
        )
        return await create(
            db,
            source.workspace_id,
            {
                "name": title.strip(),
                "description": description,
                "dataset": changes.get("dataset", source.dataset),
                "configurations": configs,
                "axes": changes.get("axes"),
                "n_runs_per_cell": repeats,
            },
            created_by=created_by,
            lineage=(source.id, source.revision, "new_conditions"),
        )
    store = store_for_product()
    cloned = Experiment(
        id=uuid.uuid4(),
        workspace_id=source.workspace_id,
        name=title.strip(),
        description=description,
        status="draft",
        dataset=deepcopy(source.dataset),
        dataset_cases=deepcopy(source.dataset_cases),
        configurations=live,
        matrix_spec={
            "configurations": [
                {key: value for key, value in config.items() if key in AXES | {"label"}}
                for config in live
            ],
            "axes": None,
        },
        n_runs_per_cell=repeats,
        max_parallel=1,
        eval_config=deepcopy(source.eval_config),
        created_by=created_by,
        cloned_from_id=source.id,
        cloned_from_revision=source.revision,
        clone_mode="frozen",
    )
    db.add(cloned)
    await db.flush()
    for config in live:
        for case in source.dataset_cases:
            try:
                frozen = await load_plan(
                    db,
                    store,
                    source.workspace_id,
                    source.id,
                    config["config_key"],
                    case["case_key"],
                    trial_id=uuid.uuid4(),
                    task_id=uuid.uuid4(),
                )
                require_pinned_runtime(frozen.private)
            except ProtocolError as error:
                raise ValueError(str(error)) from None
            original = await db.scalar(
                select(BenchmarkExperimentPlan).where(
                    BenchmarkExperimentPlan.workspace_id == source.workspace_id,
                    BenchmarkExperimentPlan.experiment_id == source.id,
                    BenchmarkExperimentPlan.config_key == config["config_key"],
                    BenchmarkExperimentPlan.case_key == case["case_key"],
                )
            )
            await freeze_plan(
                db,
                store,
                cloned,
                config["config_key"],
                case["case_key"],
                frozen,
                source_plan_id=original.id,
            )
    cloned.input_fingerprint = experiment_input_fingerprint(cloned)
    await db.commit()
    await db.refresh(cloned)
    return cloned
