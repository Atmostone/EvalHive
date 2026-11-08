"""Fork a frozen execution into a new trial without replacing historical results."""

import uuid
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.benchmarks.experiment_plans import condition_digest, rebind_payload, require_pinned_runtime
from app.benchmarks.jobs import find_request
from app.benchmarks.launch import enqueue_frozen_execution
from app.benchmarks.launch_inputs import FrozenInputs, refresh_authorization, scoped
from app.benchmarks.trials import failure
from app.models.benchmark import BenchmarkDatasetCase, BenchmarkDatasetRevision, BenchmarkProfile
from app.models.benchmark_job import BenchmarkJob
from app.models.benchmark_trial import BenchmarkTrial
from app.models.harness_profile import HarnessProfile
from app.models.task import Task


async def queue_retry(db, store, workspace, source_id, request_id):
    request_id = uuid.UUID(str(request_id))
    document = {"kind": "execution", "retry_of_id": str(source_id)}
    previous = await find_request(db, workspace.id, request_id, document)
    if previous is not None:
        return previous, await db.get(BenchmarkTrial, previous.trial_id)
    source = await db.scalar(
        select(BenchmarkTrial).where(
            BenchmarkTrial.id == source_id,
            BenchmarkTrial.workspace_id == workspace.id,
        )
    )
    if source is None:
        raise failure("SELECTION_UNAVAILABLE", "Source benchmark execution is unavailable")
    if source.phase != "finished" or source.cleanup_status != "complete":
        raise failure("SOURCE_ACTIVE", "Retry requires a finished execution with completed cleanup")
    originals = list(
        await db.scalars(
            select(BenchmarkJob)
            .where(
                BenchmarkJob.trial_id == source.id,
                BenchmarkJob.workspace_id == workspace.id,
                BenchmarkJob.kind == "execution",
            )
            .options(undefer(BenchmarkJob.private_launch))
            .limit(2)
        )
    )
    if len(originals) != 1 or originals[0].status not in {"completed", "error", "cancelled"}:
        raise failure("SOURCE_SPEC_UNAVAILABLE", "The frozen execution request is unavailable")
    original = originals[0]
    try:
        payload = store.private.open("benchmark-job:" + str(original.id), original.private_launch)
        require_pinned_runtime(payload)
        selection = source.snapshot["selection"]
        for model, key in [
            (BenchmarkProfile, "profile_id"),
            (BenchmarkDatasetRevision, "dataset_revision_id"),
            (HarnessProfile, "harness_profile_id"),
        ]:
            await scoped(db, model, workspace.id, selection[key])
        # Cases inherit workspace scope from their immutable dataset revision.
        case = await db.get(BenchmarkDatasetCase, uuid.UUID(selection["case_id"]))
        if case is None or str(case.dataset_revision_id) != selection["dataset_revision_id"]:
            raise failure("SELECTION_UNAVAILABLE", "Frozen benchmark case is unavailable")
        original_condition = condition_digest(
            FrozenInputs(
                source.snapshot, payload, "", "", uuid.UUID(selection["harness_profile_id"])
            )
        )
        trial_id, task_id = uuid.uuid4(), uuid.uuid4()
        payload = rebind_payload(payload, workspace.id, trial_id=trial_id, task_id=task_id)
        payload = await refresh_authorization(db, workspace.id, source.snapshot, payload)
        source_task = await db.get(Task, source.task_id) if source.task_id else None
        frozen = FrozenInputs(
            deepcopy(source.snapshot),
            payload,
            source_task.title if source_task else payload["benchmark_spec"]["case"]["title"],
            source_task.description
            if source_task
            else payload["benchmark_spec"]["case"].get("description"),
            uuid.UUID(selection["harness_profile_id"]),
        )
        if condition_digest(frozen) != original_condition:
            raise failure(
                "SOURCE_CONDITIONS_CHANGED", "Retry changed the frozen execution conditions"
            )
    except (KeyError, ValueError, TypeError):
        raise failure(
            "SOURCE_SPEC_UNAVAILABLE", "Frozen execution conditions cannot be authenticated"
        ) from None
    return await enqueue_frozen_execution(
        db,
        store,
        workspace,
        request_id,
        document,
        frozen,
        trial_id=trial_id,
        task_id=task_id,
        retry_of_id=source.id,
        replay_of_task_id=source.task_id,
        priority=source_task.priority if source_task else "medium",
    )
