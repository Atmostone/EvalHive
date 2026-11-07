"""Create a product task, native trial and durable launch request in one transaction."""

import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.benchmarks.jobs import enqueue, find_request
from app.benchmarks.launch_inputs import resolve_inputs
from app.models.benchmark_trial import BenchmarkTrial
from app.models.task import Task


async def queue_execution(db, store, workspace, request, *, request_document=None, origin="user"):
    document = {
        "kind": "execution",
        **(request_document if request_document is not None else request),
    }
    document.pop("request_id", None)
    request_id = uuid.UUID(str(request["request_id"]))
    previous = await find_request(db, workspace.id, request_id, document)
    if previous is not None:
        trial = await db.get(BenchmarkTrial, previous.trial_id)
        return previous, trial
    trial_id, task_id = uuid.uuid4(), uuid.uuid4()
    limits = request["limits"]
    frozen = await resolve_inputs(
        db,
        workspace,
        request,
        trial_id=trial_id,
        task_id=task_id,
        limits=limits,
        agent_max_turns=request["agent_max_turns"],
    )
    deadline = await db.scalar(select(func.clock_timestamp())) + timedelta(
        seconds=limits["wall_time_seconds"]
    )
    task = Task(
        id=task_id,
        workspace_id=workspace.id,
        title=request.get("title") or frozen.title[:500],
        description=frozen.description,
        priority=request["priority"],
        status="ready",
        origin=origin,
        harness_profile_id=frozen.harness_profile_id,
        execution_mode="direct",
        max_retries=0,
    )
    db.add(task)
    await db.flush()
    trial = await store.create_pending(
        db,
        workspace.id,
        trial_id=trial_id,
        snapshot=frozen.snapshot,
        deadline_at=deadline,
        task_id=task_id,
        dataset_case_id=uuid.UUID(str(request["case_id"])),
        profile_id=uuid.UUID(str(request["profile_id"])),
    )
    job = await enqueue(
        db,
        store,
        workspace.id,
        trial_id,
        request_id,
        document,
        frozen.private,
        deadline_at=deadline,
    )
    task.run_config = {
        "benchmark_job_id": str(job.id),
        "benchmark_trial_id": str(trial_id),
        "benchmark_mode": True,
        "execution_mode": "direct",
    }
    await db.flush()
    return job, trial
