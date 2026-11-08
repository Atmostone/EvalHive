"""Regrade lifecycle owns its verdict and cleanup, never the finished source trial."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.benchmarks.commands import CommandStore
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_evaluation_control import BenchmarkEvaluationControl
from app.models.benchmark_trial import (
    BenchmarkHostReservation,
    BenchmarkResourceLease,
    BenchmarkTrial,
)
from app.runtime.admission import GroupPolicy, HostCapacity, ResourceDemand
from evalhive_benchmark import ProtocolError, command
from evalhive_benchmark.protocol import response
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.test_benchmark_evaluations import native_result
from tests.integration.test_benchmark_trials import POLICY, QUOTA, WORKSPACE
from tests.integration.test_evaluation_controls import controls as _controls
from tests.integration.test_evaluation_controls import driver as _driver
from tests.integration.test_evaluation_resources import resources as _resources

controls, driver, resources = _controls, _driver, _resources


async def source_state(store, source):
    async with AsyncSession(store.engine) as db:
        row = await db.get(BenchmarkTrial, source.trial_id)
        attempt = await db.get(BenchmarkEvaluationAttempt, source.id)
        return (
            row.phase,
            row.next_seq,
            row.execution_status,
            row.evaluation_status,
            row.cleanup_status,
            row.recording_status,
            row.finished_at,
            attempt.result_digest,
        )


async def attempt(store, owner):
    async with AsyncSession(store.engine) as db:
        return await db.get(BenchmarkEvaluationAttempt, owner.control_id)


async def preparing(store, owner):
    await store.start_planning(owner)

    async def measure():
        return HostCapacity(8192, 8192, 4, datetime.now(UTC).isoformat())

    await store.reserve(
        owner,
        demands=(ResourceDemand("worker", QUOTA, 256, 0.1, 10),),
        native_resources={},
        measure=measure,
        runtime_policy=POLICY,
        group_policy=GroupPolicy(),
    )
    await store.transition(owner, "planning", "preparing")
    await store.transition(owner, "preparing", "ready")


async def evaluating(store, owner, *, actor=None):
    await preparing(store, owner)
    await store.transition(owner, "ready", "evaluating")
    actor = actor or uuid.uuid5(owner.controller_id, "benchmark.actor")
    await store.register_stream(owner, actor)
    return actor


async def confirmed(store, owner, actor, result):
    ledger = CommandStore(store, owner, actor)
    frame = command(
        "evaluation." + str(owner.control_id),
        "evaluate",
        {
            "scope": {
                "workspace_id": str(owner.workspace_id),
                "trial_id": str(owner.trial_id),
                "generation": owner.generation,
            },
            "payload": {},
        },
    )
    ticket = await ledger.begin(frame)
    await ledger.receive(ticket.id, response(frame["id"], result))
    await ledger.complete(ticket.id, event_floor=-1)
    await ledger.acknowledge(ticket.id)
    return ledger


@pytest.mark.parametrize("correct", [True, False])
async def test_completed_verdict_and_duplicate_finish_leave_source_unchanged(controls, correct):
    _, store, source, owner, _ = controls
    before = await source_state(store, source)
    actor = await evaluating(store, owner)
    result = native_result(correct)
    await confirmed(store, owner, actor, result)
    await store.finish_evaluation(owner, result, attempt_id=owner.control_id)
    await store.seal_stream(owner, actor, -1)
    await store.transition(owner, "evaluating", "cleaning")
    await store.transition(owner, "cleaning", "finished")
    state = await store.control(owner)
    assert (
        state.execution_status,
        state.evaluation_status,
        state.cleanup_status,
        state.recording_status,
    ) == ("not_applicable", "completed", "complete", "complete")
    native = await attempt(store, owner)
    assert native.success is correct and native.verdict == ("correct" if correct else "incorrect")
    async with AsyncSession(store.engine) as db:
        before_seq = (await db.get(BenchmarkEvaluationControl, owner.control_id)).next_seq
    await store.finish_evaluation(owner, result, attempt_id=owner.control_id)
    assert (await store.transition(owner, "cleaning", "finished"))["duplicate"]
    async with AsyncSession(store.engine) as db:
        assert (await db.get(BenchmarkEvaluationControl, owner.control_id)).next_seq == before_seq
        assert await db.get(BenchmarkHostReservation, store.host_id) is None
    assert await source_state(store, source) == before
    with pytest.raises(ProtocolError) as closed:
        await store.note(owner, "controller.observed")
    assert closed.value.code == "TRIAL_FINISHED"


async def test_result_requires_own_intent_actor_and_exact_durable_receipt(controls):
    _, store, _, owner, _ = controls
    actor = await evaluating(store, owner)
    result = native_result()
    with pytest.raises(ProtocolError) as missing:
        await store.finish_evaluation(owner, result, attempt_id=owner.control_id)
    assert missing.value.code == "COMMAND_OPEN"
    with pytest.raises(ProtocolError) as foreign:
        await store.finish_evaluation(owner, result, attempt_id=uuid.uuid4())
    assert foreign.value.code == "EVALUATION_NOT_FOUND"
    other = uuid.uuid4()
    await store.register_stream(owner, other)
    await confirmed(store, owner, other, result)
    with pytest.raises(ProtocolError) as wrong_actor:
        await store.finish_evaluation(owner, result, attempt_id=owner.control_id)
    assert wrong_actor.value.code == "COMMAND_OPEN"
    await confirmed(store, owner, actor, result)
    with pytest.raises(ProtocolError) as changed:
        await store.finish_evaluation(owner, native_result(False), attempt_id=owner.control_id)
    assert changed.value.code == "EVALUATION_CONFLICT"
    assert (await attempt(store, owner)).status == "running"


async def test_phase_graph_and_group_admission_cannot_start_an_agent(controls):
    _, store, _, owner, _ = controls
    await store.start_planning(owner)
    with pytest.raises(ProtocolError) as unreserved:
        await store.transition(owner, "planning", "preparing")
    assert unreserved.value.code == "NO_RESERVATION"
    with pytest.raises(ProtocolError) as driving:
        await store.transition(owner, "planning", "driving")
    assert driving.value.code == "INVALID_PHASE"
    assert (await store.control(owner)).phase == "planning"


@pytest.mark.parametrize("reason", ["cancel", "deadline"])
async def test_committed_stop_prevents_a_successful_verdict(controls, reason):
    _, store, source, owner, _ = controls
    before = await source_state(store, source)
    deadline = None
    if reason == "deadline":
        deadline = datetime.now(UTC) + timedelta(seconds=5)
        owner = await store.create(
            WORKSPACE, source.trial_id, source.id, uuid.uuid4(), deadline_at=deadline
        )
    actor = await evaluating(store, owner)
    result = native_result()
    await confirmed(store, owner, actor, result)
    if reason == "cancel":
        assert await store.request_stop(owner)
    else:
        await asyncio.sleep(max(0, (deadline - datetime.now(UTC)).total_seconds()) + 0.05)
    with pytest.raises(ProtocolError) as stopping:
        await store.finish_evaluation(owner, result, attempt_id=owner.control_id)
    assert stopping.value.code == "TRIAL_STOPPING"
    await store.transition(owner, "evaluating", "cleaning")
    assert (await attempt(store, owner)).success is None
    assert (await attempt(store, owner)).status == ("cancelled" if reason == "cancel" else "error")
    assert await source_state(store, source) == before


async def test_cleanup_error_does_not_overwrite_native_verdict(controls):
    _, store, _, owner, _ = controls
    actor = await evaluating(store, owner)
    result = native_result()
    await confirmed(store, owner, actor, result)
    await store.finish_evaluation(owner, result, attempt_id=owner.control_id)
    await store.seal_stream(owner, actor, -1)
    await store.note(owner, "cleanup.failed", cleanup_error=True)
    await store.transition(owner, "evaluating", "cleaning")
    await store.transition(owner, "cleaning", "finished")
    assert (await store.control(owner)).cleanup_status == "error"
    assert (await attempt(store, owner)).success is True


async def test_finish_waits_for_own_leases_and_retains_another_context_reservation(resources):
    store, source, owner, scope, _, _, _, old_handle, _, _ = resources
    await store.transition(owner, "planning", "preparing")
    await store.transition(owner, "preparing", "cleaning")
    with pytest.raises(ProtocolError) as pending:
        await store.transition(owner, "cleaning", "finished")
    assert pending.value.code == "CLEANUP_PENDING"
    sibling = await store.create(
        WORKSPACE,
        source.trial_id,
        source.id,
        uuid.uuid4(),
        deadline_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    await store.register_stream(sibling, uuid.uuid4())
    async with store.engine.begin() as db:
        await db.execute(
            update(BenchmarkResourceLease)
            .where(BenchmarkResourceLease.session_id == uuid.UUID(scope.session_id))
            .values(state="released")
        )
        await db.execute(
            update(BenchmarkResourceLease)
            .where(BenchmarkResourceLease.id == uuid.UUID(old_handle))
            .values(state="allocated")
        )
        await db.execute(
            update(BenchmarkHostReservation)
            .where(BenchmarkHostReservation.host_id == store.host_id)
            .values(controller_id=sibling.controller_id)
        )
    await store.transition(owner, "cleaning", "finished")
    assert (await store.control(owner)).recording_status == "complete"
    async with AsyncSession(store.engine) as db:
        reservation = await db.get(BenchmarkHostReservation, store.host_id)
        assert reservation.controller_id == sibling.controller_id


@pytest.mark.parametrize("field", ["phase", "finished_at", "cleanup_status", "recording_status"])
async def test_finished_evaluator_lifecycle_is_database_immutable(controls, field):
    _, store, _, owner, _ = controls
    await store.transition(owner, "created", "cleaning")
    await store.transition(owner, "cleaning", "finished")
    changes = (
        {"phase": "created", "finished_at": None}
        if field == "phase"
        else {
            field: {
                "finished_at": datetime.now(UTC),
                "cleanup_status": "error",
                "recording_status": "incomplete",
            }[field]
        }
    )
    async with store.engine.begin() as db:
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(
                update(BenchmarkEvaluationControl)
                .where(BenchmarkEvaluationControl.id == owner.control_id)
                .values(**changes)
            )


async def test_lost_controller_cannot_cancel_its_replacement(controls):
    _, store, source, owner, _ = controls
    async with store.engine.begin() as db:
        await db.execute(
            update(BenchmarkEvaluationControl)
            .where(BenchmarkEvaluationControl.id == owner.control_id)
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    current = await store.claim(WORKSPACE, source.trial_id, owner.control_id, uuid.uuid4())
    with pytest.raises(ProtocolError) as stale:
        await store.request_stop(owner)
    assert stale.value.code == "STALE_GENERATION"
    assert not (await store.control(current)).cancelled


async def test_evaluator_usage_has_its_own_scope_and_does_not_charge_source(controls):
    from app.benchmarks.usage import evaluation_usage, trial_usage

    _, store, source, owner, _ = controls
    actor = uuid.uuid4()
    await store.start_planning(owner)
    await store.register_stream(owner, actor)
    frames = [
        {
            "protocol_version": "1.0",
            "type": "event",
            "id": "usage-" + str(sequence),
            "scope": {
                "workspace_id": str(WORKSPACE),
                "trial_id": str(owner.trial_id),
                "generation": 1,
            },
            "seq": sequence,
            "source": "native-grader",
            "event": "usage",
            "data": {
                "usage_id": "same-native-call",
                "role": "grader",
                "model": "fixture",
                "purpose": "grading",
                "mode": "cumulative",
                "input_tokens": tokens,
            },
        }
        for sequence, tokens in enumerate([2, 5, 5])
    ]
    await store.ingest(owner, actor, frames)
    await store.ingest(owner, actor, frames)
    original = await trial_usage(store, WORKSPACE, source.trial_id)
    assert original["groups"] == []
    summary = await evaluation_usage(store, WORKSPACE, source.trial_id, owner.control_id)
    assert summary["groups"][0]["tokens"]["input_tokens"]["value"] == 5
    assert summary["groups"][0]["calls"] == 1 and not summary["measurement_complete"]
    assert summary["groups"][0]["cost"]["amount"] is None
    for workspace, trial in [(uuid.uuid4(), source.trial_id), (WORKSPACE, uuid.uuid4())]:
        with pytest.raises(ProtocolError) as foreign:
            await evaluation_usage(store, workspace, trial, owner.control_id)
        assert foreign.value.code == "EVALUATION_NOT_FOUND"
    sibling = await store.create(
        WORKSPACE,
        source.trial_id,
        source.id,
        uuid.uuid4(),
        deadline_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    await store.register_stream(sibling, actor)
    await store.ingest(sibling, actor, frames)
    sibling_usage = await evaluation_usage(store, WORKSPACE, source.trial_id, sibling.control_id)
    assert sibling_usage["groups"] == summary["groups"]
    assert (await evaluation_usage(store, WORKSPACE, source.trial_id, owner.control_id)) == summary
