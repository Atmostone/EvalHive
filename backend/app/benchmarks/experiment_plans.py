"""Freeze native matrix inputs once, then bind fresh execution identities."""

import uuid
from copy import deepcopy

from evalhive_benchmark import validate_spec
from evalhive_benchmark.peer import identity
from evalhive_harness import validate_run
from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.benchmarks.launch_inputs import FrozenInputs
from app.benchmarks.trials import failure
from app.models.benchmark_experiment_plan import BenchmarkExperimentPlan
from app.models.experiment import Experiment


def condition_digest(frozen):
    """Connection values affecting behavior are bound; credentials are excluded."""
    payload = frozen.private
    return identity(
        {
            "snapshot": frozen.snapshot,
            "benchmark_manifest": payload["benchmark_manifest"],
            "harness_manifest": payload["harness_manifest"],
            "agent_model": payload["harness_spec"]["model"],
            "auxiliary_models": {
                role: {name: value[name] for name in ("name", "endpoint", "phases")}
                for role, value in payload["auxiliary_models"].items()
            },
        }
    )


async def freeze_plan(db, store, experiment, config_key, case_key, frozen):
    if experiment.status != "draft":
        raise failure("EXPERIMENT_ACTIVE", "Native matrix inputs freeze before execution")
    if frozen.private["benchmark_spec"]["scope"]["workspace_id"] != str(
        experiment.workspace_id
    ) or frozen.private["harness_spec"]["workspace_id"] != str(experiment.workspace_id):
        raise failure("INVALID_SCOPE", "Frozen native inputs belong to another workspace")
    identity_id = uuid.uuid4()
    row = BenchmarkExperimentPlan(
        id=identity_id,
        workspace_id=experiment.workspace_id,
        experiment_id=experiment.id,
        config_key=config_key,
        case_key=case_key,
        condition_digest=condition_digest(frozen),
        snapshot=deepcopy(frozen.snapshot),
        private_inputs=store.private.seal(
            "benchmark-experiment-plan:" + str(identity_id),
            {
                "payload": frozen.private,
                "title": frozen.title,
                "description": frozen.description,
                "harness_profile_id": str(frozen.harness_profile_id),
            },
        ),
    )
    db.add(row)
    await db.flush()
    return row


async def load_plan(
    db, store, workspace_id, experiment_id, config_key, case_key, *, trial_id, task_id
):
    row = await db.scalar(
        select(BenchmarkExperimentPlan)
        .join(
            Experiment,
            (Experiment.id == BenchmarkExperimentPlan.experiment_id)
            & (Experiment.workspace_id == BenchmarkExperimentPlan.workspace_id),
        )
        .where(
            BenchmarkExperimentPlan.workspace_id == workspace_id,
            BenchmarkExperimentPlan.experiment_id == experiment_id,
            BenchmarkExperimentPlan.config_key == config_key,
            BenchmarkExperimentPlan.case_key == case_key,
        )
        .options(undefer(BenchmarkExperimentPlan.private_inputs))
    )
    if row is None:
        raise failure("PLAN_UNAVAILABLE", "Frozen native matrix inputs are unavailable")
    values = store.private.open("benchmark-experiment-plan:" + str(row.id), row.private_inputs)
    payload = deepcopy(values["payload"])
    payload["benchmark_spec"]["scope"] = {
        "workspace_id": str(workspace_id),
        "trial_id": str(trial_id),
        "generation": 1,
    }
    harness = payload["harness_spec"]
    harness["workspace_id"], harness["task_id"], harness["attempt_id"] = (
        str(workspace_id),
        str(task_id),
        str(uuid.uuid4()),
    )
    harness["metadata"]["benchmark_trial_id"] = str(trial_id)
    validate_spec(payload["benchmark_manifest"], payload["benchmark_spec"])
    validate_run(payload["harness_manifest"], harness)
    frozen = FrozenInputs(
        deepcopy(row.snapshot),
        payload,
        values["title"],
        values["description"],
        uuid.UUID(values["harness_profile_id"]),
    )
    if condition_digest(frozen) != row.condition_digest:
        raise failure("PLAN_CONFLICT", "Frozen native matrix conditions differ from their digest")
    return frozen
