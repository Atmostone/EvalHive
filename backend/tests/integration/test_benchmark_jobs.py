"""Queued launch identity and encrypted inputs survive separate database sessions."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.benchmarks.jobs import JobStore, enqueue, find_request
from app.benchmarks.trials import Controller
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_job import BenchmarkJob
from app.models.benchmark_trial import BenchmarkTrial
from evalhive_benchmark import ProtocolError
from sqlalchemy import inspect, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.test_benchmark_trial_api import public_trial as public_trial


def launch(store, trial, workspace_id, *, request_id=None, kind="execution", source=None):
    identity = uuid.uuid4()
    return BenchmarkJob(
        id=identity,
        workspace_id=workspace_id,
        trial_id=trial.id,
        request_id=request_id or uuid.uuid4(),
        kind=kind,
        source_evaluation_id=source,
        request_digest="a" * 64,
        private_launch=store.private.seal(
            "benchmark-job:" + str(identity), {"credential": "synthetic-private-launch"}
        ),
        deadline_at=trial.deadline_at,
    )


async def test_launch_request_is_durable_private_and_unique_per_workspace(public_trial, _engine):
    store, trial, workspace_id = public_trial
    row = launch(store, trial, workspace_id)
    identity, request_id = row.id, row.request_id
    async with AsyncSession(_engine) as db:
        db.add(row)
        await db.commit()
    async with AsyncSession(_engine) as db:
        restored = await db.get(BenchmarkJob, identity)
        assert restored.status == "queued" and "private_launch" in inspect(restored).unloaded
        db.add(launch(store, trial, workspace_id, request_id=request_id))
        with pytest.raises(IntegrityError):
            await db.commit()


@pytest.mark.parametrize(
    "change",
    [
        {"request_digest": "b" * 64},
        {"private_launch": b"changed"},
        {"request_id": uuid.UUID(int=100)},
        {"deadline_at": datetime(2030, 1, 1, tzinfo=UTC)},
    ],
)
async def test_launch_inputs_cannot_change_after_queueing(public_trial, _engine, change):
    store, trial, workspace_id = public_trial
    row = launch(store, trial, workspace_id)
    identity = row.id
    async with AsyncSession(_engine) as db:
        db.add(row)
        await db.commit()
    async with AsyncSession(_engine) as db:
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(
                update(BenchmarkJob).where(BenchmarkJob.id == identity).values(**change)
            )


async def test_launch_cancellation_and_terminal_receipts_cannot_be_rewritten(public_trial, _engine):
    store, trial, workspace_id = public_trial
    row = launch(store, trial, workspace_id)
    identity = row.id
    async with AsyncSession(_engine) as db:
        db.add(row)
        await db.commit()
    async with AsyncSession(_engine) as db:
        await db.execute(
            update(BenchmarkJob)
            .where(BenchmarkJob.id == identity)
            .values(status="running", cancel_requested_at=datetime.now(UTC))
        )
        await db.commit()
    for change in ({"cancel_requested_at": None}, {"status": "queued"}):
        async with AsyncSession(_engine) as db:
            with pytest.raises(DBAPIError):
                await db.execute(
                    update(BenchmarkJob).where(BenchmarkJob.id == identity).values(**change)
                )
    async with AsyncSession(_engine) as db:
        await db.execute(
            update(BenchmarkJob)
            .where(BenchmarkJob.id == identity)
            .values(status="cancelled", finished_at=datetime.now(UTC))
        )
        await db.commit()
    async with AsyncSession(_engine) as db:
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(
                update(BenchmarkJob).where(BenchmarkJob.id == identity).values(error_code="CHANGED")
            )


async def test_regrade_job_requires_an_evaluation_from_the_same_trial(public_trial, _engine):
    store, trial, workspace_id = public_trial
    async with AsyncSession(_engine) as db:
        source = await db.scalar(
            select(BenchmarkEvaluationAttempt.id).where(
                BenchmarkEvaluationAttempt.trial_id == trial.id
            )
        )
        db.add(launch(store, trial, workspace_id, kind="regrade", source=source))
        await db.commit()
    for kind, candidate_source in (
        ("regrade", None),
        ("regrade", uuid.uuid4()),
        ("execution", source),
    ):
        async with AsyncSession(_engine) as db:
            db.add(launch(store, trial, workspace_id, kind=kind, source=candidate_source))
            with pytest.raises(IntegrityError):
                await db.commit()


async def test_enqueue_is_atomic_and_retries_preserve_the_original_frozen_inputs(
    public_trial, _engine
):
    store, _, workspace_id = public_trial
    trial = await store.create(
        workspace_id, snapshot={}, deadline_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    request_id, document = uuid.uuid4(), {"case_id": "original", "config": {"limit": 2}}
    async with AsyncSession(_engine, expire_on_commit=False) as db:
        row = await enqueue(
            db,
            store,
            workspace_id,
            trial.id,
            request_id,
            document,
            {"credential": "original-private-input"},
            deadline_at=trial.deadline_at,
        )
        job_id = row.id
        await db.commit()
    async with AsyncSession(_engine) as db:
        existing = await find_request(db, workspace_id, request_id, document)
        assert existing.id == job_id
        again = await enqueue(
            db,
            store,
            workspace_id,
            trial.id,
            request_id,
            document,
            {"credential": "rotated-private-input"},
            deadline_at=trial.deadline_at,
        )
        assert again.id == job_id
    async with AsyncSession(_engine) as db:
        with pytest.raises(ProtocolError) as conflict:
            await find_request(db, workspace_id, request_id, {"case_id": "changed"})
        assert conflict.value.code == "LAUNCH_CONFLICT"
    owner = await store.claim(workspace_id, trial.id, uuid.uuid4())
    jobs = JobStore(store)
    await jobs.start(owner, job_id)
    assert await jobs.inputs(owner, job_id) == {"credential": "original-private-input"}
    stranger = Controller(workspace_id, trial.id, owner.generation, uuid.uuid4())
    with pytest.raises(ProtocolError) as fenced:
        await jobs.inputs(stranger, job_id)
    assert fenced.value.code == "STALE_GENERATION"
    with pytest.raises(ProtocolError) as unfinished:
        await jobs.finish(owner, job_id, status="completed")
    assert unfinished.value.code == "LAUNCH_OPEN"
    async with AsyncSession(_engine) as db:
        await db.execute(
            update(BenchmarkTrial)
            .where(BenchmarkTrial.id == trial.id)
            .values(phase="finished", finished_at=datetime.now(UTC))
        )
        await db.commit()
    await jobs.finish(owner, job_id, status="error", error_code="CANCELLED")
    await jobs.finish(owner, job_id, status="error", error_code="CANCELLED")
    with pytest.raises(ProtocolError) as changed:
        await jobs.finish(owner, job_id, status="completed")
    assert changed.value.code == "LAUNCH_CONFLICT"
