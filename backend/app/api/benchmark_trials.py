"""Workspace-scoped public trial journals, verdict history and cancellation."""

from __future__ import annotations

import uuid
from contextlib import AsyncExitStack
from typing import Literal

from evalhive_benchmark import ProtocolError
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.background import BackgroundTask

from app.api.benchmark_sdk import SafeValidationRoute
from app.auth.dependencies import get_current_workspace, require_role
from app.benchmarks.evidence import CHUNK_BYTES, _io, reference
from app.benchmarks.launch import queue_execution
from app.benchmarks.launch_schema import TrialLimits
from app.benchmarks.private_store import PrivateStore
from app.benchmarks.trials import TrialStore
from app.benchmarks.usage import trial_usage
from app.config import get_settings
from app.database import get_db
from app.models.benchmark_artifact import BenchmarkArtifact
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_trial import BenchmarkJournalEntry, BenchmarkTrial

router = APIRouter(
    prefix="/api/benchmark-trials", tags=["benchmark-trials"], route_class=SafeValidationRoute
)
OPERATOR = Depends(require_role("owner", "admin", "member"))


def get_trial_store():
    from app import database

    secret = get_settings().jwt_secret
    if not secret:
        raise HTTPException(503, "Private benchmark storage is not configured")
    return TrialStore(database.engine, PrivateStore(secret))


def get_public_evidence(store=Depends(get_trial_store)):
    from app.benchmarks.product_runtime import evidence_store

    try:
        return evidence_store(store)
    except Exception:
        raise HTTPException(503, "Benchmark evidence storage is unavailable") from None


class TrialCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    profile_id: uuid.UUID
    dataset_revision_id: uuid.UUID
    case_id: uuid.UUID
    harness_profile_id: uuid.UUID
    title: str | None = Field(default=None, min_length=1, max_length=500, pattern=r"\S")
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    limits: TrialLimits = Field(default_factory=TrialLimits)
    agent_max_turns: int = Field(default=100, ge=1, le=1000000)


@router.post("", status_code=202, dependencies=[OPERATOR])
async def create_trial(
    body: TrialCreate,
    workspace=Depends(get_current_workspace),
    db=Depends(get_db),
    store=Depends(get_trial_store),
):
    try:
        job, trial = await queue_execution(
            db,
            store,
            workspace,
            body.model_dump(mode="json"),
            request_document=body.model_dump(mode="json", exclude_unset=True),
        )
        await db.commit()
        return {**trial_dict(trial), "job_id": str(job.id), "job_status": job.status}
    except ProtocolError as error:
        await db.rollback()
        status = (
            404
            if error.code == "SELECTION_UNAVAILABLE"
            else 409
            if error.code == "LAUNCH_CONFLICT"
            else 422
        )
        raise HTTPException(status, str(error)) from None
    except (ValueError, IntegrityError):
        await db.rollback()
        raise HTTPException(
            422, "Benchmark launch configuration is invalid or has changed"
        ) from None


async def scoped_trial(db, workspace, trial_id):
    trial = await db.scalar(
        select(BenchmarkTrial).where(
            BenchmarkTrial.id == trial_id, BenchmarkTrial.workspace_id == workspace.id
        )
    )
    if trial is None:
        raise HTTPException(404, "Benchmark trial not found")
    return trial


def trial_dict(trial):
    return {
        "id": str(trial.id),
        "task_id": str(trial.task_id) if trial.task_id else None,
        "dataset_case_id": str(trial.dataset_case_id) if trial.dataset_case_id else None,
        "profile_id": str(trial.profile_id) if trial.profile_id else None,
        "retry_of_id": str(trial.retry_of_id) if trial.retry_of_id else None,
        "snapshot": trial.snapshot,
        "phase": trial.phase,
        "execution_status": trial.execution_status,
        "evaluation_status": trial.evaluation_status,
        "recording_status": trial.recording_status,
        "cleanup_status": trial.cleanup_status,
        "cancel_requested_at": trial.cancel_requested_at,
        "deadline_at": trial.deadline_at,
        "created_at": trial.created_at,
        "finished_at": trial.finished_at,
        "next_seq": trial.next_seq,
    }


def evaluation_dict(attempt):
    return {
        "id": str(attempt.id),
        "trial_id": str(attempt.trial_id),
        "source_evaluation_id": str(attempt.source_evaluation_id)
        if attempt.source_evaluation_id
        else None,
        "kind": attempt.kind,
        "status": attempt.status,
        "verdict": attempt.verdict,
        "success": attempt.success,
        "metrics": attempt.metrics,
        "primary_metric": attempt.primary_metric,
        "error_kind": attempt.error_kind,
        "input_digest": attempt.input_digest,
        "result_digest": attempt.result_digest,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
    }


@router.get("")
async def list_trials(
    task_id: uuid.UUID | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    workspace=Depends(get_current_workspace),
    db=Depends(get_db),
):
    query = select(BenchmarkTrial).where(BenchmarkTrial.workspace_id == workspace.id)
    if task_id is not None:
        query = query.where(BenchmarkTrial.task_id == task_id)
    rows = await db.scalars(
        query.order_by(BenchmarkTrial.created_at.desc(), BenchmarkTrial.id)
        .offset(offset)
        .limit(limit)
    )
    return [trial_dict(row) for row in rows]


@router.get("/{trial_id}")
async def read_trial(
    trial_id: uuid.UUID, workspace=Depends(get_current_workspace), db=Depends(get_db)
):
    return trial_dict(await scoped_trial(db, workspace, trial_id))


@router.get("/{trial_id}/events")
async def read_events(
    trial_id: uuid.UUID,
    after_seq: int = Query(-1, ge=-1),
    limit: int = Query(100, ge=1, le=500),
    workspace=Depends(get_current_workspace),
    db=Depends(get_db),
):
    trial = await scoped_trial(db, workspace, trial_id)
    rows = list(
        await db.scalars(
            select(BenchmarkJournalEntry)
            .where(
                BenchmarkJournalEntry.trial_id == trial.id,
                BenchmarkJournalEntry.seq > after_seq,
            )
            .order_by(BenchmarkJournalEntry.seq)
            .limit(limit)
        )
    )
    return {
        "events": [
            {
                "seq": row.seq,
                "event": row.event,
                "data": row.public_data,
                "created_at": row.created_at,
            }
            for row in rows
        ],
        "next_seq": rows[-1].seq if rows else after_seq,
    }


@router.get("/{trial_id}/evaluations")
async def read_evaluations(
    trial_id: uuid.UUID,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    workspace=Depends(get_current_workspace),
    db=Depends(get_db),
):
    await scoped_trial(db, workspace, trial_id)
    rows = await db.scalars(
        select(BenchmarkEvaluationAttempt)
        .where(
            BenchmarkEvaluationAttempt.trial_id == trial_id,
            BenchmarkEvaluationAttempt.workspace_id == workspace.id,
        )
        .order_by(BenchmarkEvaluationAttempt.started_at, BenchmarkEvaluationAttempt.id)
        .offset(offset)
        .limit(limit)
    )
    return [evaluation_dict(row) for row in rows]


@router.get("/{trial_id}/artifacts")
async def read_artifacts(
    trial_id: uuid.UUID,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    workspace=Depends(get_current_workspace),
    db=Depends(get_db),
):
    await scoped_trial(db, workspace, trial_id)
    rows = await db.scalars(
        select(BenchmarkArtifact)
        .where(
            BenchmarkArtifact.trial_id == trial_id,
            BenchmarkArtifact.workspace_id == workspace.id,
            BenchmarkArtifact.visibility.in_(("user", "agent")),
        )
        .order_by(BenchmarkArtifact.created_at, BenchmarkArtifact.id)
        .offset(offset)
        .limit(limit)
    )
    return [
        {"id": str(row.id), "artifact": reference(row), "created_at": row.created_at}
        for row in rows
    ]


@router.get("/{trial_id}/artifacts/{artifact_id}/download")
async def download_artifact(
    trial_id: uuid.UUID,
    artifact_id: uuid.UUID,
    workspace=Depends(get_current_workspace),
    db=Depends(get_db),
    evidence=Depends(get_public_evidence),
):
    await scoped_trial(db, workspace, trial_id)
    row = await db.scalar(
        select(BenchmarkArtifact).where(
            BenchmarkArtifact.id == artifact_id,
            BenchmarkArtifact.trial_id == trial_id,
            BenchmarkArtifact.workspace_id == workspace.id,
            BenchmarkArtifact.visibility.in_(("user", "agent")),
        )
    )
    if row is None:
        raise HTTPException(404, "Benchmark artifact not found")
    artifact = reference(row)
    lifetime = AsyncExitStack()
    try:
        spool = await lifetime.enter_async_context(
            evidence.open_verified(
                workspace.id, trial_id, artifact, visibilities={"user", "agent"}, db=db
            )
        )
    except (ProtocolError, ValueError, OSError):
        await lifetime.aclose()
        raise HTTPException(
            409, "Benchmark artifact bytes are unavailable or failed verification"
        ) from None
    except BaseException:
        await lifetime.aclose()
        raise

    async def chunks():
        try:
            while data := await _io(lambda: spool.read(CHUNK_BYTES)):
                yield data
        finally:
            await lifetime.aclose()

    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{artifact_id}.bin"',
            "Content-Length": str(artifact["size_bytes"]),
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(lifetime.aclose),
    )


@router.get("/{trial_id}/usage")
async def read_usage(
    trial_id: uuid.UUID,
    workspace=Depends(get_current_workspace),
    db=Depends(get_db),
    store=Depends(get_trial_store),
):
    await scoped_trial(db, workspace, trial_id)
    try:
        return await trial_usage(store, workspace.id, trial_id)
    except (ProtocolError, ValueError):
        raise HTTPException(409, "Benchmark usage evidence is unavailable or incomplete") from None


@router.post("/{trial_id}/cancel", dependencies=[OPERATOR])
async def cancel_trial(
    trial_id: uuid.UUID,
    workspace=Depends(get_current_workspace),
    db=Depends(get_db),
    store=Depends(get_trial_store),
):
    await scoped_trial(db, workspace, trial_id)
    requested = await store.request_cancel(workspace.id, trial_id)
    return {"requested": requested}


class RegradeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: uuid.UUID
    source_evaluation_id: uuid.UUID
    wall_time_seconds: int = Field(default=300, ge=1, le=86400)


def evaluation_job_dict(job, control=None):
    return {
        "id": str(job.id),
        "request_id": str(job.request_id),
        "trial_id": str(job.trial_id),
        "source_evaluation_id": str(job.source_evaluation_id),
        "status": job.status,
        "error_code": job.error_code,
        "cancel_requested_at": job.cancel_requested_at,
        "created_at": job.created_at,
        "deadline_at": job.deadline_at,
        "finished_at": job.finished_at,
        "evaluation_id": str(control.id) if control is not None else None,
        "phase": control.phase
        if control is not None
        else job.status
        if job.status in {"completed", "error", "cancelled"}
        else "queued",
        "recording_status": control.recording_status if control is not None else None,
        "cleanup_status": control.cleanup_status if control is not None else None,
    }


@router.post("/{trial_id}/regrade", status_code=202, dependencies=[OPERATOR])
async def regrade_trial(
    trial_id: uuid.UUID,
    body: RegradeCreate,
    workspace=Depends(get_current_workspace),
    db=Depends(get_db),
    store=Depends(get_trial_store),
    evidence=Depends(get_public_evidence),
):
    from app.benchmarks.regrade_launch import queue_regrade

    try:
        job = await queue_regrade(
            db,
            store,
            evidence,
            workspace,
            trial_id,
            body.model_dump(mode="json"),
            request_document=body.model_dump(mode="json", exclude_unset=True),
        )
        from app.models.benchmark_evaluation_control import BenchmarkEvaluationControl

        control = await db.get(BenchmarkEvaluationControl, job.id)
        await db.commit()
        return evaluation_job_dict(job, control)
    except ProtocolError as error:
        await db.rollback()
        status = (
            404
            if error.code in {"SELECTION_UNAVAILABLE", "EVALUATION_NOT_FOUND"}
            else 409
            if error.code in {"LAUNCH_CONFLICT", "SOURCE_ACTIVE"}
            else 422
        )
        raise HTTPException(status, str(error)) from None
    except (ValueError, IntegrityError):
        await db.rollback()
        raise HTTPException(422, "Retained benchmark evaluation inputs are unavailable") from None


async def scoped_evaluation_job(db, workspace, trial_id, job_id):
    from app.models.benchmark_job import BenchmarkJob

    job = await db.scalar(
        select(BenchmarkJob).where(
            BenchmarkJob.id == job_id,
            BenchmarkJob.trial_id == trial_id,
            BenchmarkJob.workspace_id == workspace.id,
            BenchmarkJob.kind == "regrade",
        )
    )
    if job is None:
        raise HTTPException(404, "Benchmark evaluation request not found")
    return job


@router.get("/{trial_id}/evaluation-jobs")
async def list_evaluation_jobs(
    trial_id: uuid.UUID,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    workspace=Depends(get_current_workspace),
    db=Depends(get_db),
):
    from app.models.benchmark_evaluation_control import BenchmarkEvaluationControl
    from app.models.benchmark_job import BenchmarkJob

    await scoped_trial(db, workspace, trial_id)
    rows = await db.execute(
        select(BenchmarkJob, BenchmarkEvaluationControl)
        .outerjoin(
            BenchmarkEvaluationControl,
            BenchmarkEvaluationControl.id == BenchmarkJob.id,
        )
        .where(
            BenchmarkJob.trial_id == trial_id,
            BenchmarkJob.workspace_id == workspace.id,
            BenchmarkJob.kind == "regrade",
        )
        .order_by(BenchmarkJob.created_at, BenchmarkJob.id)
        .offset(offset)
        .limit(limit)
    )
    return [evaluation_job_dict(job, control) for job, control in rows]


@router.post("/{trial_id}/evaluation-jobs/{job_id}/cancel", dependencies=[OPERATOR])
async def cancel_evaluation_job(
    trial_id: uuid.UUID,
    job_id: uuid.UUID,
    workspace=Depends(get_current_workspace),
    db=Depends(get_db),
):
    from sqlalchemy import func, update

    from app.models.benchmark_evaluation_control import BenchmarkEvaluationControl
    from app.models.benchmark_job import BenchmarkJob

    job = await scoped_evaluation_job(db, workspace, trial_id, job_id)
    if job.status in {"completed", "error", "cancelled"}:
        return {"requested": False}
    # Match the controller -> queue lock order used by dispatch. A queued job
    # has no control yet; dispatch propagates its durable cancellation flag.
    await db.execute(
        update(BenchmarkEvaluationControl)
        .where(
            BenchmarkEvaluationControl.id == job_id,
            BenchmarkEvaluationControl.workspace_id == workspace.id,
            BenchmarkEvaluationControl.source_trial_id == trial_id,
            BenchmarkEvaluationControl.phase != "finished",
        )
        .values(
            cancel_requested_at=func.coalesce(
                BenchmarkEvaluationControl.cancel_requested_at, func.clock_timestamp()
            )
        )
    )
    changed = await db.execute(
        update(BenchmarkJob)
        .where(
            BenchmarkJob.id == job_id,
            BenchmarkJob.workspace_id == workspace.id,
            BenchmarkJob.status.in_(("queued", "running")),
        )
        .values(
            cancel_requested_at=func.coalesce(
                BenchmarkJob.cancel_requested_at, func.clock_timestamp()
            )
        )
    )
    await db.commit()
    return {"requested": changed.rowcount == 1}
