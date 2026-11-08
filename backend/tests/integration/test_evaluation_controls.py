"""A grading lease and journal must never reopen the source execution."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.benchmarks.evaluation_controls import EvaluationControls
from app.benchmarks.trials import current_trial
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_evaluation_control import (
    BenchmarkEvaluationControl,
    BenchmarkEvaluationJournal,
)
from app.models.benchmark_trial import BenchmarkTrial
from evalhive_benchmark import ProtocolError
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.test_benchmark_driver import driver as _driver_fixture
from tests.integration.test_benchmark_evaluations import attempt_row
from tests.integration.test_benchmark_trials import WORKSPACE

driver = _driver_fixture


async def test_creation_retry_reads_saved_receipt_before_external_state(controls, monkeypatch):
    from unittest.mock import AsyncMock

    _, repository, source, owner, deadline = controls
    lookup = AsyncMock(
        side_effect=AssertionError("A creation receipt must not re-read external evidence")
    )
    monkeypatch.setattr("app.benchmarks.evaluation_controls.retained_source", lookup)
    assert (
        await repository.create(
            WORKSPACE,
            source.trial_id,
            source.id,
            owner.controller_id,
            deadline_at=deadline,
            evaluation_id=owner.control_id,
        )
        == owner
    )
    lookup.assert_not_called()


@pytest_asyncio.fixture
async def controls(driver):
    controller, spec = driver
    spec["config"] = {"checkpoint": True}
    await controller.run(spec)
    source = await attempt_row(controller)
    repository = EvaluationControls(controller.store, None)
    deadline = datetime.now(UTC) + timedelta(minutes=2)
    owner = await repository.create(
        WORKSPACE, source.trial_id, source.id, uuid.uuid4(), deadline_at=deadline
    )
    return controller, repository, source, owner, deadline


async def test_new_context_uses_own_attempt_and_sequence(controls):
    controller, repository, source, owner, deadline = controls
    async with AsyncSession(repository.engine) as db:
        before = (await db.get(BenchmarkTrial, source.trial_id)).next_seq
    await repository.note(owner, "controller.observed", private={"native": "private-data"})
    await repository.heartbeat(owner)
    assert (
        await repository.create(
            WORKSPACE,
            source.trial_id,
            source.id,
            owner.controller_id,
            deadline_at=deadline,
            evaluation_id=owner.control_id,
        )
        == owner
    )
    async with AsyncSession(repository.engine) as db:
        row, _ = await repository.current(db, owner)
        assert row.next_seq == 2 and row.phase == "created"
        source_trial = await db.get(BenchmarkTrial, source.trial_id)
        assert source_trial.phase == "finished" and source_trial.next_seq == before
        attempt = await db.get(BenchmarkEvaluationAttempt, owner.control_id)
        assert attempt.kind == "regrade" and attempt.source_evaluation_id == source.id
        entries = (
            await db.scalars(
                select(BenchmarkEvaluationJournal).order_by(BenchmarkEvaluationJournal.seq)
            )
        ).all()
        assert [entry.seq for entry in entries] == [0, 1]
        assert "private-data" not in str([entry.public_data for entry in entries])
        with pytest.raises(ProtocolError) as wrong:
            await current_trial(db, owner)
        assert wrong.value.code == "FOREIGN_CONTROL"


async def test_independent_cancel_cannot_cancel_source(controls):
    _, repository, source, owner, _ = controls
    assert not await repository.trials.request_cancel(WORKSPACE, source.trial_id)
    assert not await repository.request_cancel(uuid.uuid4(), source.trial_id, owner.control_id)
    assert await repository.request_cancel(WORKSPACE, source.trial_id, owner.control_id)
    async with AsyncSession(repository.engine) as db:
        row, _ = await repository.current(db, owner)
        first = row.cancel_requested_at
    assert await repository.request_cancel(WORKSPACE, source.trial_id, owner.control_id)
    async with AsyncSession(repository.engine) as db:
        row, _ = await repository.current(db, owner)
        assert row.cancel_requested_at == first
        assert (await db.get(BenchmarkTrial, source.trial_id)).cancel_requested_at is None


async def test_expired_claim_fences_old_owner_and_does_not_resume(controls):
    from app.benchmarks.regrade import retained_source

    _, repository, source, owner, _ = controls
    with pytest.raises(ProtocolError) as live:
        await repository.claim(WORKSPACE, source.trial_id, owner.control_id, uuid.uuid4())
    assert live.value.code == "OWNER_ACTIVE"
    async with repository.engine.begin() as db:
        await db.execute(
            update(BenchmarkEvaluationControl)
            .where(BenchmarkEvaluationControl.id == owner.control_id)
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    recovered = await repository.claim(WORKSPACE, source.trial_id, owner.control_id, uuid.uuid4())
    assert recovered.generation == 2
    with pytest.raises(ProtocolError) as stale:
        await repository.note(owner, "controller.observed")
    assert stale.value.code == "STALE_GENERATION"
    with pytest.raises(ProtocolError):
        await repository.heartbeat(owner)
    async with AsyncSession(repository.engine) as db:
        row, _ = await repository.current(db, recovered)
        assert row.phase == "cleaning"
        attempt = await db.get(BenchmarkEvaluationAttempt, owner.control_id)
        assert (
            attempt.status == "error"
            and attempt.success is None
            and attempt.error_kind == "uncertain"
        )
        assert (await db.get(BenchmarkEvaluationAttempt, source.id)).status == "completed"
    saved = await retained_source(
        repository.trials, None, WORKSPACE, source.trial_id, owner.control_id
    )
    assert saved["checkpoint"]["state"] == {"captured": True}


@pytest.mark.parametrize("field", ["workspace_id", "trial_id", "control_id"])
async def test_context_cannot_cross_scope(controls, field):
    _, repository, _, owner, _ = controls
    foreign = replace(owner, **{field: uuid.uuid4()})
    with pytest.raises(ProtocolError):
        await repository.note(foreign, "controller.observed")


@pytest.mark.parametrize(
    "model,values",
    [
        (BenchmarkEvaluationControl, {"deadline_at": datetime.now(UTC) + timedelta(days=1)}),
        (BenchmarkEvaluationJournal, {"public_data": {"changed": True}}),
    ],
)
async def test_origin_and_journal_are_database_immutable(controls, model, values):
    _, repository, _, _, _ = controls
    async with repository.engine.begin() as db:
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(update(model).values(**values))
