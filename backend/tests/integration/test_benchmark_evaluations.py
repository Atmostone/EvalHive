"""Confirmed native verdicts, immutable evaluator inputs and independent failures."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.benchmarks.evaluation import history
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_trial import BenchmarkTrial
from evalhive_benchmark import ProtocolError
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from tests.integration.test_benchmark_driver import driver as _driver_fixture
from tests.integration.test_benchmark_driver import wait_for_phase
from tests.integration.test_benchmark_trials import WORKSPACE

driver = _driver_fixture


@pytest.mark.parametrize("checkpoint", [True, False])
async def test_regrade_source_is_verified_without_reopening_execution(driver, checkpoint):
    from app.benchmarks.regrade import retained_source

    controller, spec = driver
    spec["config"] = {"checkpoint": checkpoint}
    await controller.run(spec)
    row = await attempt_row(controller)
    async with AsyncSession(controller.store.engine) as db:
        before = (await db.get(BenchmarkTrial, controller.owner.trial_id)).next_seq
    if checkpoint:
        source = await retained_source(controller.store, None, WORKSPACE, row.trial_id, row.id)
        assert source["checkpoint"]["state"] == {"captured": True}
        assert source["models"] == {} and source["credential_refs"] == {}
        assert "prepared" not in source and "execution" not in source
    else:
        with pytest.raises(ProtocolError) as unavailable:
            await retained_source(controller.store, None, WORKSPACE, row.trial_id, row.id)
        assert unavailable.value.code == "REGRADE_UNAVAILABLE"
    with pytest.raises(ProtocolError) as foreign:
        await retained_source(controller.store, None, uuid.uuid4(), row.trial_id, row.id)
    assert foreign.value.code == "EVALUATION_NOT_FOUND"
    async with AsyncSession(controller.store.engine) as db:
        trial = await db.get(BenchmarkTrial, row.trial_id)
        assert trial.phase == "finished" and trial.next_seq == before


async def attempt_row(controller):
    async with AsyncSession(controller.store.engine, expire_on_commit=False) as db:
        return await db.scalar(
            select(BenchmarkEvaluationAttempt)
            .where(
                BenchmarkEvaluationAttempt.trial_id == controller.owner.trial_id,
            )
            .options(
                undefer(BenchmarkEvaluationAttempt.private_inputs),
                undefer(BenchmarkEvaluationAttempt.private_result),
            )
        )


def native_result(correct=True):
    return {
        "status": "completed",
        "success": correct,
        "metrics": [{"name": "success", "value": int(correct)}],
        "native": {"correct": correct},
        "artifacts": [],
        "completeness": {"state": "captured"},
    }


@pytest.mark.parametrize("correct", [True, False])
async def test_native_correctness_is_separate_from_execution(driver, correct):
    controller, spec = driver
    spec["config"] = {"correct": correct, "private_marker": "evaluator-private-input"}
    result = await controller.run(spec)
    row = await attempt_row(controller)
    assert result["execution"] == "success" and result["evaluation"] == "completed"
    assert row.success is correct and row.verdict == ("correct" if correct else "incorrect")
    assert row.finished_at is not None and row.status == "completed"
    assert b"evaluator-private-input" not in row.private_inputs
    inputs = controller.store.private.open("evaluation-inputs:" + str(row.id), row.private_inputs)
    assert inputs["spec"]["config"]["effective_marker"] == "resolved"
    assert inputs["spec"]["config"]["private_marker"] == "evaluator-private-input"
    summaries = await history(controller.store, WORKSPACE, controller.owner.trial_id)
    assert len(summaries) == 1 and summaries[0]["outcome"] == row.verdict
    assert "evaluator-private-input" not in json.dumps(summaries)


async def test_repeated_begin_and_finish_replay_without_changing_history(driver):
    controller, spec = driver
    await controller.run(spec)
    row = await attempt_row(controller)
    inputs = controller.store.private.open("evaluation-inputs:" + str(row.id), row.private_inputs)
    result = controller.store.private.open("evaluation-result:" + str(row.id), row.private_result)
    async with AsyncSession(controller.store.engine) as db:
        before = (await db.get(BenchmarkTrial, controller.owner.trial_id)).next_seq
    assert await controller.store.begin_evaluation(controller.owner, inputs) == row.id
    await controller.store.finish_evaluation(controller.owner, result, attempt_id=row.id)
    async with AsyncSession(controller.store.engine) as db:
        assert (await db.get(BenchmarkTrial, controller.owner.trial_id)).next_seq == before
    with pytest.raises(ProtocolError) as changed:
        await controller.store.finish_evaluation(
            controller.owner, native_result(False), attempt_id=row.id
        )
    assert changed.value.code == "EVALUATION_CONFLICT"
    inputs["spec"]["config"]["changed"] = True
    with pytest.raises(ProtocolError) as changed_input:
        await controller.store.begin_evaluation(controller.owner, inputs)
    assert changed_input.value.code == "EVALUATION_CONFLICT"


@pytest.mark.parametrize(
    "field, value",
    [("input_digest", "a" * 64), ("private_result", b"tampered"), ("success", False)],
)
async def test_evaluator_inputs_and_outcome_are_database_immutable(driver, field, value):
    controller, spec = driver
    await controller.run(spec)
    async with controller.store.engine.begin() as connection:
        with pytest.raises(DBAPIError, match="immutable"):
            await connection.execute(update(BenchmarkEvaluationAttempt).values(**{field: value}))


@pytest.mark.parametrize(
    "config,outcome",
    [({"fail_phase": "evaluate"}, "grading_error"), ({"die_phase": "evaluate"}, "infra_error")],
)
async def test_checker_crash_and_incorrect_answer_are_different(driver, config, outcome):
    controller, spec = driver
    spec["config"] = config
    result = await controller.run(spec)
    summaries = await history(controller.store, WORKSPACE, controller.owner.trial_id)
    assert result["execution"] == "success" and summaries[0]["outcome"] == outcome
    assert summaries[0]["verdict"] == "unknown" and summaries[0]["success"] is None


async def test_result_requires_its_confirmed_command_receipt(driver):
    controller, spec = driver
    spec["config"] = {"wait_phase": "evaluate"}
    pending = asyncio.create_task(controller.run(spec))
    try:
        await wait_for_phase(controller.store, controller.owner.trial_id, "evaluate", pending)
        row = await attempt_row(controller)
        with pytest.raises(ProtocolError) as missing:
            await controller.store.finish_evaluation(
                controller.owner, native_result(), attempt_id=row.id
            )
        assert missing.value.code == "COMMAND_OPEN"
        assert (await attempt_row(controller)).status == "running"
        await controller.store.request_cancel(WORKSPACE, controller.owner.trial_id)
        await asyncio.wait_for(pending, 15)
        summaries = await history(controller.store, WORKSPACE, controller.owner.trial_id)
        assert summaries[0]["outcome"] == "cancelled" and summaries[0]["success"] is None
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_changed_value_cannot_be_substituted_for_durable_receipt(driver):
    controller, spec = driver
    original = controller.store.finish_evaluation

    async def substituted(owner, result=None, **kwargs):
        if result is not None:
            result = {**result, "native": {"substituted": True}}
        return await original(owner, result, **kwargs)

    controller.store.finish_evaluation = substituted
    result = await controller.run(spec)
    assert result["evaluation"] == "error"
    assert (await attempt_row(controller)).success is None


async def test_owner_takeover_records_lost_evaluation_without_accepting_old_verdict(driver):
    controller, spec = driver
    spec["config"] = {"wait_phase": "evaluate"}
    pending = asyncio.create_task(controller.run(spec))
    try:
        await wait_for_phase(controller.store, controller.owner.trial_id, "evaluate", pending)
        async with controller.store.engine.begin() as connection:
            await connection.execute(
                update(BenchmarkTrial)
                .where(BenchmarkTrial.id == controller.owner.trial_id)
                .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        claimed = await controller.store.claim(WORKSPACE, controller.owner.trial_id, uuid.uuid4())
        assert (await asyncio.wait_for(pending, 12))["controller"] == "detached"
        row = await attempt_row(controller)
        assert row.status == "error" and row.error_kind == "uncertain" and row.success is None
        with pytest.raises(ProtocolError) as stale:
            await controller.store.finish_evaluation(
                controller.owner, native_result(), attempt_id=row.id
            )
        assert stale.value.code == "STALE_GENERATION"
        await controller.store.transition(claimed, "cleaning", "finished")
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_evaluator_history_is_workspace_scoped(driver):
    controller, spec = driver
    await controller.run(spec)
    with pytest.raises(ProtocolError) as foreign:
        await history(controller.store, uuid.uuid4(), controller.owner.trial_id)
    assert foreign.value.code == "TRIAL_NOT_FOUND"


async def test_cancel_committed_before_verdict_cannot_become_success(driver):
    controller, spec = driver
    original = controller.store.finish_evaluation

    async def cancel_before_commit(owner, result=None, **kwargs):
        if result is not None:
            await controller.store.request_cancel(owner.workspace_id, owner.trial_id)
        return await original(owner, result, **kwargs)

    controller.store.finish_evaluation = cancel_before_commit
    result = await controller.run(spec)
    row = await attempt_row(controller)
    assert result["evaluation"] == "cancelled" and result["execution"] == "success"
    assert row.status == "cancelled" and row.success is None and row.verdict == "unknown"
