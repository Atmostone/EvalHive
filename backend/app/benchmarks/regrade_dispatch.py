"""Dispatch evaluator-only jobs with independent leases, recovery and completion."""

import asyncio
import uuid

from evalhive_benchmark import ProtocolError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.benchmarks.dispatch import fail_unstarted, renew_owner
from app.benchmarks.evaluation_controls import EvaluationControls
from app.benchmarks.jobs import TERMINAL, JobStore
from app.benchmarks.product_runtime import ProductRuntime, evidence_store
from app.benchmarks.trials import Controller, database_fence
from app.config import get_settings
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_evaluation_control import (
    BenchmarkEvaluationControl,
    BenchmarkEvaluationJournal,
)
from app.models.benchmark_job import BenchmarkJob


async def finish_regrade_request(store, owner, job_id):
    async with database_fence(store.engine, owner.control_id) as db:
        control, _ = await store.current(db, owner, allow_expired=True)
        if control.phase != "finished":
            return False
        attempt = await db.get(BenchmarkEvaluationAttempt, owner.control_id)
        failed = await db.scalar(
            select(BenchmarkEvaluationJournal.public_data)
            .where(
                BenchmarkEvaluationJournal.control_id == owner.control_id,
                BenchmarkEvaluationJournal.event == "controller.failed",
            )
            .order_by(BenchmarkEvaluationJournal.seq.desc())
            .limit(1)
        )
        code = failed.get("code") if failed else None
        if attempt.status == "cancelled":
            status = "cancelled"
        elif (
            attempt.status in {"completed", "unavailable"}
            and control.cleanup_status == "complete"
            and control.recording_status == "complete"
        ):
            status = "completed"
        else:
            status, code = "error", code or "EVALUATION_LIFECYCLE_FAILED"
    await JobStore(store).finish(owner, job_id, status=status, error_code=code)
    return True


async def claim_request(trials, store, job_id):
    # Serialize creation only. The independent controller lease protects the
    # actual run; an existing active owner is never replayed by a second caller.
    async with database_fence(trials.engine, job_id) as db:
        job = await db.get(BenchmarkJob, job_id)
        if job is None or job.kind != "regrade" or job.status in TERMINAL:
            return None, False
        control = await db.get(BenchmarkEvaluationControl, job_id)
        if control is None:
            now = await db.scalar(select(func.clock_timestamp()))
            if job.cancel_requested_at is not None or job.deadline_at <= now:
                job.status = "cancelled" if job.cancel_requested_at is not None else "error"
                job.error_code = "CANCELLED" if job.status == "cancelled" else "DEADLINE_EXCEEDED"
                job.finished_at = now
                await db.commit()
                return None, True
            try:
                owner = await store.create(
                    job.workspace_id,
                    job.trial_id,
                    job.source_evaluation_id,
                    uuid.uuid4(),
                    deadline_at=job.deadline_at,
                    evaluation_id=job.id,
                )
            except (ProtocolError, ValueError, KeyError) as error:
                # No control/resource exists when source authorization fails.
                job.status = "error"
                job.error_code = (
                    error.code if isinstance(error, ProtocolError) else "SOURCE_SPEC_UNAVAILABLE"
                )
                job.finished_at = await db.scalar(select(func.clock_timestamp()))
                await db.commit()
                return None, True
            return owner, False
        if control.phase == "finished":
            return Controller(
                job.workspace_id, job.trial_id, control.generation, control.controller_id, job.id
            ), False
        workspace_id, trial_id = job.workspace_id, job.trial_id
    # Recovery takes the established host/trial lock order, outside creation's
    # job fence. The control repository atomically refuses an active owner.
    try:
        return await store.claim(workspace_id, trial_id, job_id, uuid.uuid4()), False
    except ProtocolError as error:
        if error.code == "OWNER_ACTIVE":
            return None, True
        raise


async def run_regrade_job(trials, job_id, *, runtime_factory=None):
    store = EvaluationControls(trials, evidence_store(trials))
    owner, handled = await claim_request(trials, store, job_id)
    if owner is None:
        return handled
    if await finish_regrade_request(store, owner, job_id):
        return True
    jobs = JobStore(store)
    await jobs.start(owner, job_id)
    stopped = asyncio.Event()
    renewal = asyncio.create_task(renew_owner(store, owner, stopped))
    runtime = None
    try:
        async with AsyncSession(store.engine) as db:
            job = await db.get(BenchmarkJob, job_id)
            cancelled = job.cancel_requested_at is not None
        state = await store.control(owner)
        recovered = state.phase == "cleaning"
        if cancelled or (not get_settings().benchmark_runtime_enabled and not recovered):
            await store.request_cancel(owner.workspace_id, owner.trial_id, job_id)
            state = await store.control(owner)
        if state.phase == "created" and state.cancelled:
            await fail_unstarted(store, owner, "CANCELLED")
        elif state.phase == "created" and state.seconds_remaining <= 0:
            await fail_unstarted(store, owner, "DEADLINE_EXCEEDED")
        else:
            factory = runtime_factory or ProductRuntime.installed
            runtime = await asyncio.to_thread(factory, store, recovery=recovered)
            if recovered:
                await runtime.recovery(owner).run()
            else:
                payload = await jobs.inputs(owner, job_id)
                controller = await runtime.evaluation(owner, payload)
                await controller.run()
        await finish_regrade_request(store, owner, job_id)
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


async def record_failure(store, owner, job_id, code):
    state = await store.control(owner)
    if state.phase == "finished" or await fail_unstarted(store, owner, code):
        await finish_regrade_request(store, owner, job_id)
    else:
        await store.note(owner, "cleanup.failed", code=code, incomplete=True)
