"""Drain durable benchmark jobs through the shared coordinator, one job at a time."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from datetime import UTC

from evalhive_benchmark import ProtocolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.benchmarks.jobs import JobStore
from app.benchmarks.private_store import PrivateStore
from app.benchmarks.product_runtime import ProductRuntime
from app.benchmarks.trials import Controller, TrialStore, database_fence
from app.config import get_settings
from app.models.benchmark_job import BenchmarkJob
from app.models.benchmark_trial import (
    BenchmarkHostReservation,
    BenchmarkJournalEntry,
    BenchmarkTrial,
)
from app.models.task import Task
from app.runtime import get_process_runtime
from app.utils.failures import FAILURE_INFRA


def product_store():
    from app import database

    return TrialStore(database.engine, PrivateStore(get_settings().jwt_secret))


async def renew_owner(store, owner, stopped):
    while not stopped.is_set():
        await store.heartbeat(owner)
        with suppress(TimeoutError):
            await asyncio.wait_for(stopped.wait(), max(1, store.lease_seconds / 3))


async def finish_request(store, owner, job_id):
    async with database_fence(store.engine, owner.trial_id) as db:
        trial, _ = await store.current(db, owner, allow_expired=True)
        if trial.phase != "finished":
            return False
        code = await db.scalar(
            select(BenchmarkJournalEntry.public_data)
            .where(
                BenchmarkJournalEntry.trial_id == trial.id,
                BenchmarkJournalEntry.event == "controller.failed",
            )
            .order_by(BenchmarkJournalEntry.seq.desc())
            .limit(1)
        )
        failed_code = code.get("code") if code else None
        if trial.execution_status == "cancelled":
            status = "cancelled"
        elif (
            trial.execution_status == "success"
            and trial.cleanup_status == "complete"
            and trial.recording_status == "complete"
            and trial.evaluation_status in {"completed", "unavailable"}
        ):
            status = "completed"
        else:
            status = "error"
        if status == "error" and failed_code is None:
            failed_code = "BENCHMARK_LIFECYCLE_FAILED"
        if trial.task_id is not None:
            task = await db.get(Task, trial.task_id)
            if task is not None and task.workspace_id == trial.workspace_id:
                task.status = "done" if trial.execution_status == "success" else "failed"
                task.completed_at = (
                    trial.finished_at.astimezone(UTC).replace(tzinfo=None)
                    if trial.finished_at is not None
                    else None
                )
                if (
                    task.current_attempt_id is None
                    and task.status == "failed"
                    and task.failure_type is None
                ):
                    task.failure_type = FAILURE_INFRA
                if not task.result_summary:
                    task.result_summary = "Benchmark execution finished. Native evaluation and cleanup are recorded separately."
        await db.commit()
    await JobStore(store).finish(owner, job_id, status=status, error_code=failed_code)
    return True


async def fail_unstarted(store, owner, code):
    """A factory failure has no resource leases; never use this for live workers."""
    state = await store.control(owner)
    if state.phase != "created":
        return False
    await store.note(owner, "controller.failed", code=code)
    await store.transition(owner, "created", "cleaning")
    await store.finish_evaluation(owner)
    await store.transition(owner, "cleaning", "finished")
    return True


async def record_failure(store, owner, job_id, code):
    state = await store.control(owner)
    if state.phase == "finished" or await fail_unstarted(store, owner, code):
        await finish_request(store, owner, job_id)
    else:
        await store.note(owner, "cleanup.failed", code=code, incomplete=True)


async def run_execution_job(store, job_id, *, runtime_factory=None):
    factory = runtime_factory or ProductRuntime.installed
    async with AsyncSession(store.engine) as db:
        job = await db.get(BenchmarkJob, job_id)
        if (
            job is None
            or job.kind != "execution"
            or job.status in {"completed", "error", "cancelled"}
        ):
            return False
        trial = await db.get(BenchmarkTrial, job.trial_id)
        workspace_id, trial_id, snapshot = job.workspace_id, job.trial_id, trial.snapshot
        if trial.phase == "finished":
            owner = Controller(workspace_id, trial_id, trial.generation, trial.controller_id)
        else:
            owner = None
        task_id = trial.task_id
    if owner is not None:
        return await finish_request(store, owner, job_id)
    try:
        owner = await store.claim(workspace_id, trial_id, uuid.uuid4())
    except ProtocolError as error:
        if error.code == "OWNER_ACTIVE":
            return True
        raise
    jobs = JobStore(store)
    await jobs.start(owner, job_id)
    stopped = asyncio.Event()
    renewal = asyncio.create_task(renew_owner(store, owner, stopped))
    runtime = None
    try:
        state = await store.control(owner)
        recovered = state.phase == "cleaning"
        if not get_settings().benchmark_runtime_enabled and not recovered:
            await store.request_cancel(workspace_id, trial_id)
        if state.phase == "created" and (
            state.cancelled or not get_settings().benchmark_runtime_enabled
        ):
            await fail_unstarted(store, owner, "CANCELLED")
        elif state.phase == "created" and state.seconds_remaining <= 0:
            await fail_unstarted(store, owner, "DEADLINE_EXCEEDED")
        else:
            runtime = await asyncio.to_thread(factory, store, recovery=recovered)
            if recovered:
                await runtime.recovery(owner).run({})
            else:
                payload = await jobs.inputs(owner, job_id)
                controller, spec, private_inputs = await runtime.execution(owner, payload, snapshot)
                async with AsyncSession(store.engine) as db:
                    await store.current(db, owner)
                    task = await db.get(Task, task_id)
                    if task is not None:
                        task.status = "in_progress"
                        await db.commit()
                await controller.run(spec, private_inputs=private_inputs)
        await finish_request(store, owner, job_id)
    except ProtocolError as error:
        if error.code not in {"STALE_GENERATION", "OWNER_EXPIRED", "TRIAL_NOT_FOUND"}:
            await record_failure(store, owner, job_id, error.code)
    except Exception:
        await record_failure(store, owner, job_id, "RUNTIME_UNAVAILABLE")
    finally:
        stopped.set()
        await asyncio.gather(renewal, return_exceptions=True)
        if runtime is not None:
            await runtime.close()
    return True


async def benchmark_tick(*, store=None, runtime_factory=None):
    from app import database

    async with AsyncSession(store.engine if store is not None else database.engine) as db:
        job = await db.scalar(
            select(BenchmarkJob)
            .where(
                BenchmarkJob.kind == "execution",
                BenchmarkJob.status.in_(("queued", "running")),
            )
            .order_by(
                (BenchmarkJob.status == "running").desc(), BenchmarkJob.created_at, BenchmarkJob.id
            )
            .limit(1)
        )
        reserved = await db.scalar(select(BenchmarkHostReservation.host_id).limit(1))
        if job is None:
            return reserved is not None
        job_id, running = job.id, job.status == "running"
    if not running and await asyncio.to_thread(get_process_runtime().list_active):
        return True
    store = store or product_store()
    await run_execution_job(store, job_id, runtime_factory=runtime_factory)
    return True
