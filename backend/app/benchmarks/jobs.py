"""Idempotent product requests and owner-fenced access to frozen launch inputs."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta

from evalhive_benchmark.peer import identity
from evalhive_benchmark.protocol import _json_value
from sqlalchemy import func, select

from app.benchmarks.trials import database_fence, failure
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_job import BenchmarkJob
from app.models.benchmark_trial import BenchmarkTrial
from app.models.workspace import Workspace

TERMINAL = {"completed", "error", "cancelled"}


async def find_request(db, workspace_id, request_id, document):
    """Serialize one workspace's request identity before creating any task rows."""
    _json_value(document)
    if not isinstance(document, dict):
        raise failure("INVALID_LAUNCH", "A launch request requires an object")
    workspace = await db.scalar(
        select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()
    )
    if workspace is None:
        raise failure("INVALID_SCOPE", "Benchmark workspace is unavailable")
    row = await db.scalar(
        select(BenchmarkJob).where(
            BenchmarkJob.workspace_id == workspace_id, BenchmarkJob.request_id == request_id
        )
    )
    if row is not None and row.request_digest != identity(document):
        raise failure("LAUNCH_CONFLICT", "This request identity has different launch parameters")
    return row


async def enqueue(
    db,
    store,
    workspace_id,
    trial_id,
    request_id,
    document,
    launch,
    *,
    deadline_at,
    kind="execution",
    source_evaluation_id=None,
):
    """Join the caller's task/trial transaction; this function never commits it."""
    previous = await find_request(db, workspace_id, request_id, document)
    if previous is not None:
        if (previous.trial_id, previous.kind, previous.source_evaluation_id) != (
            trial_id,
            kind,
            source_evaluation_id,
        ):
            raise failure("LAUNCH_CONFLICT", "This request identity belongs to another launch")
        return previous
    _json_value(launch)
    now = await db.scalar(select(func.clock_timestamp()))
    trial = await db.scalar(
        select(BenchmarkTrial).where(
            BenchmarkTrial.id == trial_id, BenchmarkTrial.workspace_id == workspace_id
        )
    )
    if trial is None:
        raise failure("INVALID_SCOPE", "Benchmark trial is unavailable")
    if (
        not isinstance(launch, dict)
        or kind not in {"execution", "regrade"}
        or (kind == "execution") != (source_evaluation_id is None)
        or not isinstance(deadline_at, datetime)
        or deadline_at.tzinfo is None
        or not now < deadline_at <= now + timedelta(days=7)
        or (kind == "execution" and (trial.phase != "created" or deadline_at != trial.deadline_at))
    ):
        raise failure("INVALID_LAUNCH", "Benchmark launch parameters are invalid")
    if source_evaluation_id is not None:
        source = await db.scalar(
            select(BenchmarkEvaluationAttempt.id).where(
                BenchmarkEvaluationAttempt.id == source_evaluation_id,
                BenchmarkEvaluationAttempt.trial_id == trial_id,
                BenchmarkEvaluationAttempt.workspace_id == workspace_id,
            )
        )
        if source is None:
            raise failure("INVALID_SCOPE", "Benchmark evaluation is unavailable")
    job_id = uuid.uuid4()
    job = BenchmarkJob(
        id=job_id,
        workspace_id=workspace_id,
        trial_id=trial_id,
        request_id=request_id,
        source_evaluation_id=source_evaluation_id,
        kind=kind,
        request_digest=identity(document),
        private_launch=store.private.seal("benchmark-job:" + str(job_id), launch),
        deadline_at=deadline_at,
    )
    db.add(job)
    await db.flush()
    return job


class JobStore:
    """Queue state cannot substitute for a current trial/evaluation controller."""

    def __init__(self, control_store):
        self.store = control_store

    async def _owned(self, db, owner, job_id, *, allow_expired=False):
        control, now = await self.store.current(db, owner, allow_expired=allow_expired)
        row = await db.scalar(
            select(BenchmarkJob)
            .where(
                BenchmarkJob.id == job_id,
                BenchmarkJob.workspace_id == owner.workspace_id,
                BenchmarkJob.trial_id == owner.trial_id,
            )
            .with_for_update()
        )
        if row is None or ((owner.control_id is None) != (row.kind == "execution")):
            raise failure("LAUNCH_UNAVAILABLE", "Launch request belongs to another controller")
        if owner.control_id is not None:
            attempt = await db.get(BenchmarkEvaluationAttempt, owner.control_id)
            if (
                attempt is None
                or owner.control_id != row.id
                or attempt.source_evaluation_id != row.source_evaluation_id
            ):
                raise failure("LAUNCH_UNAVAILABLE", "Evaluation request belongs to another source")
        return row, control, now

    async def start(self, owner, job_id):
        async with database_fence(self.store.engine, self.store.control_key(owner)) as db:
            row, control, _ = await self._owned(db, owner, job_id)
            if row.status in TERMINAL or control.phase == "finished":
                raise failure("LAUNCH_FINISHED", "Launch request is already finished")
            row.status, row.controller_id = "running", owner.controller_id
            await db.commit()

    async def inputs(self, owner, job_id):
        async with database_fence(self.store.engine, self.store.control_key(owner)) as db:
            row, control, _ = await self._owned(db, owner, job_id)
            if (
                row.status != "running"
                or row.controller_id != owner.controller_id
                or control.phase == "finished"
            ):
                raise failure("LAUNCH_UNAVAILABLE", "Launch inputs require the active controller")
            await db.refresh(row, attribute_names=["private_launch"])
            return self.store.private.open("benchmark-job:" + str(row.id), row.private_launch)

    async def finish(self, owner, job_id, *, status, error_code=None):
        if status not in TERMINAL or (
            error_code is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", error_code)
        ):
            raise failure("INVALID_LAUNCH_RESULT", "Launch completion requires a terminal status")
        async with database_fence(self.store.engine, self.store.control_key(owner)) as db:
            row, control, now = await self._owned(db, owner, job_id, allow_expired=True)
            if control.phase != "finished" or row.controller_id not in {None, owner.controller_id}:
                raise failure(
                    "LAUNCH_OPEN", "The owned lifecycle must finish before its launch request"
                )
            if row.status in TERMINAL:
                if (row.status, row.error_code) != (status, error_code):
                    raise failure(
                        "LAUNCH_CONFLICT", "Launch completion differs from its saved result"
                    )
                return
            row.status, row.error_code, row.finished_at = status, error_code, now
            row.controller_id = owner.controller_id
            await db.commit()
