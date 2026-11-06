"""Resolve sufficient retained state without reopening an execution or granting agent keys."""

from copy import deepcopy

from evalhive_benchmark import validate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.benchmarks.trials import failure
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_trial import BenchmarkTrial


async def retained_source(store, evidence, workspace_id, trial_id, evaluation_id):
    """Private host input for a NEW evaluation controller, never an agent RunSpec.

    A connector declares native replay sufficiency. The host verifies its typed
    provenance and every retained byte; arbitrary native metadata grants nothing.
    This read cannot start a worker, mutate a trial or acquire its old resources.
    """
    async with AsyncSession(store.engine) as db:
        trial = await db.scalar(
            select(BenchmarkTrial).where(
                BenchmarkTrial.id == trial_id,
                BenchmarkTrial.workspace_id == workspace_id,
            )
        )
        attempt = await db.scalar(
            select(BenchmarkEvaluationAttempt)
            .where(
                BenchmarkEvaluationAttempt.id == evaluation_id,
                BenchmarkEvaluationAttempt.trial_id == trial_id,
                BenchmarkEvaluationAttempt.workspace_id == workspace_id,
            )
            .options(undefer(BenchmarkEvaluationAttempt.private_inputs))
        )
        if trial is None or attempt is None:
            raise failure("EVALUATION_NOT_FOUND", "Source evaluation is unavailable", "grading")
        if trial.phase != "finished" or attempt.status == "running":
            raise failure(
                "SOURCE_ACTIVE", "Regrade requires a finished source execution", "grading"
            )
        inputs = store.private.open("evaluation-inputs:" + str(attempt.id), attempt.private_inputs)
    return await verify_source(evidence, workspace_id, trial_id, evaluation_id, inputs)


async def verify_source(evidence, workspace_id, trial_id, evaluation_id, inputs):
    inputs = deepcopy(inputs)
    checkpoint = inputs["capture"].get("checkpoint")
    if checkpoint is None or inputs["evaluator"].get("capabilities", {}).get("regrade") != "native":
        raise failure(
            "REGRADE_UNAVAILABLE", "No native replay checkpoint was retained", "unsupported"
        )
    validate("regrade_checkpoint", checkpoint)
    if not checkpoint["complete"]:
        raise failure("CHECKPOINT_INCOMPLETE", "Native replay state is incomplete", "recording")
    spec, evaluator = inputs["spec"], inputs["evaluator"]
    if (
        checkpoint["connector"] != {key: evaluator[key] for key in ("id", "version")}
        or checkpoint["case"] != spec["case"]["ref"]
        or checkpoint["source_digest"] != spec["case"]["source_digest"]
        or spec["scope"]["workspace_id"] != str(workspace_id)
        or spec["scope"]["trial_id"] != str(trial_id)
    ):
        raise failure("CHECKPOINT_PROVENANCE", "Checkpoint source conditions differ", "grading")
    references = checkpoint["artifacts"]
    if len({artifact["id"] for artifact in references}) != len(references):
        raise failure("CHECKPOINT_CONFLICT", "Checkpoint repeats an artifact identity", "recording")
    for artifact in references:
        if artifact not in inputs["capture"]["artifacts"]:
            raise failure(
                "CHECKPOINT_CONFLICT", "Checkpoint references uncaptured evidence", "recording"
            )
        async with evidence.open_verified(
            workspace_id, trial_id, artifact, visibilities={"private", "evaluator", "user", "agent"}
        ):
            pass
    roles = checkpoint["evaluator_roles"]
    if any(role not in spec["models"] for role in roles):
        raise failure(
            "MODEL_BINDING_REQUIRED", "Evaluator role binding is unavailable", "configuration"
        )
    models = {role: spec["models"][role] for role in roles}
    refs = {model["credential_ref"] for model in models.values()}
    if refs - spec["credential_refs"].keys():
        raise failure(
            "CREDENTIAL_REQUIRED", "Evaluator credential reference is unavailable", "configuration"
        )
    return {
        "source_trial_id": str(trial_id),
        "source_evaluation_id": str(evaluation_id),
        "checkpoint": checkpoint,
        "case": spec["case"],
        "config": spec["config"],
        "limits": spec.get("limits"),
        "evaluator": evaluator,
        "environment_sessions": inputs["environment_sessions"],
        "models": models,
        "credential_refs": {ref: spec["credential_refs"][ref] for ref in refs},
    }
