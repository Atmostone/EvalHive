"""A fresh SDK worker restores and grades under the common supervisor, without drive."""

import asyncio
import subprocess
import sys
import uuid
from contextlib import suppress
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from app.benchmarks.async_control import bounded
from app.benchmarks.evaluation_controls import EvaluationControls
from app.benchmarks.recovery import RecoveryService
from app.benchmarks.regrade_driver import RegradeDriver
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_evaluation_control import (
    BenchmarkEvaluationControl,
    BenchmarkEvaluationJournal,
)
from app.models.benchmark_trial import BenchmarkTrial
from evalhive_benchmark import ProtocolError
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from tests.integration.test_benchmark_driver import Host, fixture_capture_validator
from tests.integration.test_benchmark_driver import driver as _driver
from tests.integration.test_benchmark_evaluations import attempt_row
from tests.integration.test_benchmark_trials import WORKSPACE

driver = _driver


async def source_snapshot(store, trial_id):
    async with AsyncSession(store.engine) as db:
        trial = await db.get(BenchmarkTrial, trial_id)
        source = await db.scalar(
            select(BenchmarkEvaluationAttempt).where(
                BenchmarkEvaluationAttempt.trial_id == trial_id,
                BenchmarkEvaluationAttempt.kind == "initial",
            )
        )
        return (
            trial.phase,
            trial.next_seq,
            trial.execution_status,
            trial.evaluation_status,
            trial.recording_status,
            trial.cleanup_status,
            source.result_digest,
        )


@pytest_asyncio.fixture
async def replay(driver, request):
    original, spec = driver
    spec["config"] = {"checkpoint": True, **getattr(request, "param", {})}
    finished = await original.run(spec)
    assert finished["phase"] == "finished" and finished["evaluation"] == "completed", finished
    source = await attempt_row(original)
    store = EvaluationControls(original.store, None)
    owner = await store.create(
        WORKSPACE,
        source.trial_id,
        source.id,
        uuid.uuid4(),
        deadline_at=datetime.now(UTC) + timedelta(seconds=45),
    )
    host = Host(store, owner)
    controller = RegradeDriver(
        host,
        RecoveryService(store, uuid.uuid4(), {}),
        capture_providers={},
        capture_validator=fixture_capture_validator,
        limits=original.limits,
    )
    before = await source_snapshot(store, source.trial_id)
    try:
        yield controller, source, before
    finally:
        if host.client:
            with suppress(ProtocolError):
                await bounded(host.client.close(0.2, shutdown=False), 3)


async def saved_attempt(controller):
    async with AsyncSession(controller.store.engine) as db:
        return await db.get(BenchmarkEvaluationAttempt, controller.owner.control_id)


async def wait_phase(controller, method, pending):
    async with asyncio.timeout(15):
        while True:
            async with AsyncSession(controller.store.engine) as db:
                entries = (
                    await db.scalars(
                        select(BenchmarkEvaluationJournal)
                        .where(
                            BenchmarkEvaluationJournal.control_id == controller.owner.control_id,
                            BenchmarkEvaluationJournal.event == "phase.started",
                        )
                        .options(undefer(BenchmarkEvaluationJournal.private_data))
                    )
                ).all()
                if any(
                    controller.store.private.open("journal:" + str(row.id), row.private_data)[
                        "data"
                    ]["phase"]
                    == method
                    for row in entries
                ):
                    return
            if pending.done():
                pytest.fail("Regrade ended before its awaited worker phase: " + str(await pending))
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("replay", [{"correct": True}, {"correct": False}], indirect=True)
async def test_frozen_checkpoint_restores_and_grades_without_agent_or_new_trial(replay):
    controller, source, before = replay
    inputs = await controller.store.launch_inputs(controller.owner)
    inputs["spec"]["config"]["correct"] = not inputs["spec"]["config"]["correct"]
    result = await controller.run()
    assert result["phase"] == "finished" and result["execution"] == "not_applicable"
    assert (
        result["evaluation"] == "completed"
        and result["cleanup"] == "complete"
        and result["recording"] == "complete"
    )
    attempt = await saved_attempt(controller)
    assert attempt.success is controller.host.installed_spec["config"]["correct"]
    assert controller.workflow.execution is None
    assert "agent" not in controller.workflow.prepared
    assert controller.workflow.prepared["native_state"] == {"captured": True}
    assert controller.host.installed_spec["purpose"] == "evaluation"
    assert await source_snapshot(controller.store, source.trial_id) == before
    async with AsyncSession(controller.store.engine) as db:
        assert await db.scalar(select(func.count()).select_from(BenchmarkTrial)) == 1
        events = (
            await db.scalars(
                select(BenchmarkEvaluationJournal)
                .where(
                    BenchmarkEvaluationJournal.control_id == controller.owner.control_id,
                    BenchmarkEvaluationJournal.event == "phase.started",
                )
                .options(undefer(BenchmarkEvaluationJournal.private_data))
            )
        ).all()
        phases = [
            controller.store.private.open("journal:" + str(row.id), row.private_data)["data"][
                "phase"
            ]
            for row in events
        ]
        assert "drive" not in phases and {"prepare", "capture", "evaluate", "cleanup"} <= set(
            phases
        )
    with pytest.raises(ValueError, match="restart or resume"):
        await controller.run()


@pytest.mark.parametrize(
    "replay",
    [
        {"regrade_fail_phase": "prepare"},
        {"regrade_fail_phase": "evaluate"},
        {"regrade_fail_phase": "cleanup"},
        {"regrade_die_phase": "evaluate"},
    ],
    indirect=True,
)
async def test_worker_failures_keep_source_and_separate_grading_from_cleanup(replay):
    controller, source, before = replay
    result = await controller.run()
    assert result["phase"] == "finished" and result["execution"] == "not_applicable"
    config = controller.source_inputs["spec"]["config"]
    cleanup_failed = config.get("regrade_fail_phase") == "cleanup"
    assert result["evaluation"] == ("completed" if cleanup_failed else "error")
    assert result["cleanup"] == ("error" if cleanup_failed else "complete")
    assert (await saved_attempt(controller)).success is (True if cleanup_failed else None)
    if config.get("regrade_die_phase"):
        assert result["recording"] == "incomplete"
    assert await source_snapshot(controller.store, source.trial_id) == before


@pytest.mark.parametrize(
    "replay",
    [{"regrade_wait_phase": phase} for phase in ["prepare", "capture", "evaluate"]],
    indirect=True,
)
async def test_cancellation_is_supervised_without_cancelling_source(replay):
    controller, source, before = replay
    inputs = await controller.store.launch_inputs(controller.owner)
    phase = inputs["spec"]["config"]["regrade_wait_phase"]
    pending = asyncio.create_task(controller.run())
    try:
        await wait_phase(controller, phase, pending)
        assert await controller.store.request_cancel(
            WORKSPACE, source.trial_id, controller.owner.control_id
        )
        result = await asyncio.wait_for(pending, 15)
        assert result["phase"] == "finished" and result["evaluation"] == "cancelled"
        assert (await saved_attempt(controller)).success is None
        assert await source_snapshot(controller.store, source.trial_id) == before
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("replay", [{"regrade_wait_phase": "prepare"}], indirect=True)
async def test_caller_cancel_waits_for_evaluator_cleanup(replay):
    controller, source, before = replay
    pending = asyncio.create_task(controller.run())
    try:
        await wait_phase(controller, "prepare", pending)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 15)
        assert (await controller.store.control(controller.owner)).phase == "finished"
        assert (await saved_attempt(controller)).status == "cancelled"
        assert await source_snapshot(controller.store, source.trial_id) == before
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("replay", [{"regrade_wait_phase": "evaluate"}], indirect=True)
async def test_takeover_detaches_old_worker_and_recovers_without_native_resume(replay):
    controller, source, before = replay
    pending = asyncio.create_task(controller.run())
    try:
        await wait_phase(controller, "evaluate", pending)
        async with controller.store.engine.begin() as db:
            await db.execute(
                update(BenchmarkEvaluationControl)
                .where(BenchmarkEvaluationControl.id == controller.owner.control_id)
                .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        owner = await controller.store.claim(
            WORKSPACE, source.trial_id, controller.owner.control_id, uuid.uuid4()
        )
        assert (await asyncio.wait_for(pending, 12))["controller"] == "detached"
        host = Host(controller.store, owner)
        recovery = RegradeDriver(
            host,
            RecoveryService(controller.store, uuid.uuid4(), {}),
            capture_providers={},
            capture_validator=fixture_capture_validator,
            limits=controller.limits,
        )
        result = await recovery.run()
        assert result["phase"] == "finished" and result["evaluation"] == "error"
        assert not host.started and result["recording"] == "incomplete"
        assert (await saved_attempt(controller)).error_kind == "uncertain"
        assert await source_snapshot(controller.store, source.trial_id) == before
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("problem", ["manifest", "evidence"])
async def test_source_or_worker_mismatch_fails_before_worker_start(replay, monkeypatch, problem):
    controller, source, before = replay
    if problem == "manifest":
        controller.host.manifest = {**deepcopy(controller.host.manifest), "image": "other-image"}
    else:
        monkeypatch.setattr(
            "app.benchmarks.evaluation_controls.verify_source",
            AsyncMock(
                side_effect=ProtocolError(
                    "ARTIFACT_READ_FAILED", "Fixture unavailable", "recording"
                )
            ),
        )
    result = await controller.run()
    assert result["phase"] == "finished" and result["evaluation"] == "error"
    assert not controller.host.started
    assert await source_snapshot(controller.store, source.trial_id) == before


@pytest.mark.parametrize("replay", [{"regrade_default_drift": True}], indirect=True)
async def test_native_validation_cannot_change_frozen_grading_configuration(replay):
    controller, source, before = replay
    result = await controller.run()
    assert result["phase"] == "finished" and result["evaluation"] == "error"
    assert (await saved_attempt(controller)).error_kind == "configuration"
    assert controller.host.started and controller.host.installed_spec is None
    assert await source_snapshot(controller.store, source.trial_id) == before


async def test_old_schema_cannot_merge_distinct_evaluator_streams(replay, _engine):
    controller, source, _ = replay
    await controller.store.register_stream(controller.owner, source.worker_actor_id)
    async with AsyncSession(_engine) as db:
        version = await db.scalar(text("SELECT version_num FROM alembic_version"))
        count = await db.scalar(text("SELECT count(*) FROM benchmark_event_streams"))
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "downgrade", "bg08"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "uq_benchmark_stream_actor" in result.stderr
    async with AsyncSession(_engine) as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == version
        assert await db.scalar(text("SELECT count(*) FROM benchmark_event_streams")) == count
        control = await db.get(BenchmarkEvaluationControl, controller.owner.control_id)
        assert control.phase == "created" and control.cleanup_status == "pending"


async def test_concurrent_creation_reuses_the_first_frozen_budget(replay, monkeypatch):
    from app.benchmarks import evaluation_controls

    controller, source, before = replay
    original = evaluation_controls.retained_source
    barrier, calls = asyncio.Barrier(2), []

    async def concurrent_source(*args, **kwargs):
        order = len(calls)
        calls.append(order)
        value = await original(*args, **kwargs)
        await barrier.wait()
        if order:
            await asyncio.sleep(1.1)
        return value

    monkeypatch.setattr(evaluation_controls, "retained_source", concurrent_source)
    evaluation_id, controller_id = uuid.uuid4(), uuid.uuid4()
    deadline = datetime.now(UTC) + timedelta(seconds=45)
    async with asyncio.timeout(10):
        owners = await asyncio.gather(
            *[
                controller.store.create(
                    WORKSPACE,
                    source.trial_id,
                    source.id,
                    controller_id,
                    deadline_at=deadline,
                    evaluation_id=evaluation_id,
                )
                for _ in range(2)
            ]
        )
    assert owners[0] == owners[1] and len(calls) == 2
    inputs = await controller.store.launch_inputs(owners[0])
    assert 40 < inputs["spec"]["limits"]["wall_time_seconds"] <= 45
    assert inputs["spec"]["limits"]["memory_mb"] == 1024
    assert await source_snapshot(controller.store, source.trial_id) == before
