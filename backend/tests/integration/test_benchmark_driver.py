"""Automatic phases and supervised faults against PostgreSQL and an SDK process."""

import asyncio
import os
import sys
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from app.benchmarks.async_control import bounded
from app.benchmarks.driver import DriverLimits, TrialDriver
from app.benchmarks.private_store import PrivateStore
from app.benchmarks.recovery import RecoveryService
from app.benchmarks.trials import TrialStore
from app.benchmarks.worker_channel import WorkerChannel
from app.models.benchmark_command import BenchmarkCommand
from app.models.benchmark_trial import (
    BenchmarkHostReservation,
    BenchmarkJournalEntry,
    BenchmarkTrial,
)
from app.runtime.admission import GroupPolicy, HostCapacity, ResourceDemand
from evalhive_benchmark import ConnectorClient, ProtocolError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import undefer

from tests.fixtures.benchmark_driver_worker import MANIFEST
from tests.integration.test_benchmark_trials import POLICY, QUOTA, WORKSPACE


class Host:
    manifest = MANIFEST

    def __init__(self, store, owner):
        self.store, self.owner, self.channel, self.client = store, owner, None, None
        self.started, self.installed_spec = False, None

    async def start(self):
        self.started = True

        async def measure():
            return HostCapacity(8192, 8192, 4, datetime.now(UTC).isoformat())

        await self.store.reserve(
            self.owner,
            demands=(ResourceDemand("worker", QUOTA, 256, 0.1, 10),),
            native_resources={},
            measure=measure,
            runtime_policy=POLICY,
            group_policy=GroupPolicy(),
        )
        worker = Path(__file__).parents[1] / "fixtures/benchmark_driver_worker.py"
        self.client = await ConnectorClient.launch(
            [sys.executable, str(worker)],
            env={"PATH": os.defpath, "PYTHONPATH": "/opt/current-benchmark-sdk"},
        )
        self.channel = await WorkerChannel.attach(self.store, self.owner, uuid.uuid4(), self.client)
        await self.channel.invoke("describe")
        return self.channel

    async def install_plan(self, spec, plan, **kwargs):
        self.installed_spec = spec

    async def detach(self, **kwargs):
        if self.channel:
            await self.channel.detach(**kwargs)
        if self.client:
            with suppress(ProtocolError):
                await self.client.close(0.2, shutdown=False)


@pytest_asyncio.fixture
async def driver(_engine, db_session):
    # Real runners reuse connections. NullPool's repeated SCRAM handshakes on
    # every heartbeat/poll otherwise dominate this one-CPU fixture controller.
    pooled = create_async_engine(_engine.url, pool_size=5, max_overflow=5)
    store = TrialStore(pooled, PrivateStore("driver-integration-fixture"), lease_seconds=10)
    trial = await store.create(
        WORKSPACE,
        snapshot={"original": True},
        deadline_at=datetime.now(UTC) + timedelta(seconds=45),
    )
    owner = await store.claim(WORKSPACE, trial.id, uuid.uuid4())
    host = Host(store, owner)
    limits = DriverLimits(
        phase_seconds=10,
        stop_seconds=1,
        capture_seconds=5,
        cleanup_seconds=10,
        service_seconds=3,
        heartbeat_seconds=0.5,
        poll_seconds=0.2,
    )
    controller = TrialDriver(
        host, RecoveryService(store, uuid.uuid4(), {}), capture_providers={}, limits=limits
    )
    spec = {
        "protocol_version": "1.0",
        "scope": {"workspace_id": str(WORKSPACE), "trial_id": str(trial.id), "generation": 1},
        "case": {
            "ref": {"dataset_id": "fixture", "revision": "1", "case_id": "driver"},
            "title": "Driver fixture",
            "source_digest": "sha256:" + "a" * 64,
            "native_ref": {},
            "requirements": {},
        },
        "config": {},
        "models": {},
        "credential_refs": {},
        "limits": {"wall_time_seconds": 60, "memory_mb": 1024, "cpu_cores": 1, "disk_mb": 64},
    }
    try:
        yield controller, spec
    finally:
        if host.client:
            with suppress(ProtocolError):
                await bounded(host.client.close(0.2, shutdown=False), 3)
        await pooled.dispose()


async def wait_for_phase(store, trial_id, method, pending=None):
    async with asyncio.timeout(15):
        while True:
            async with AsyncSession(store.engine) as db:
                rows = (
                    await db.scalars(
                        select(BenchmarkJournalEntry)
                        .where(
                            BenchmarkJournalEntry.trial_id == trial_id,
                            BenchmarkJournalEntry.event == "phase.started",
                        )
                        .options(undefer(BenchmarkJournalEntry.private_data))
                    )
                ).all()
                for entry in rows:
                    event = store.private.open("journal:" + str(entry.id), entry.private_data)
                    if event["data"].get("phase") == method:
                        return
            if pending is not None and pending.done():
                pytest.fail("Driver ended before the awaited worker phase: " + str(await pending))
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("correct", [True, False])
async def test_automatic_pipeline_applies_defaults_and_separates_correctness(
    driver, _engine, correct
):
    controller, spec = driver
    spec["config"] = {"correct": correct}
    result = await controller.run(spec)
    assert result == {
        "trial_id": str(controller.owner.trial_id),
        "phase": "finished",
        "execution": "success",
        "evaluation": "completed",
        "recording": "complete",
        "cleanup": "complete",
    }
    assert controller.host.installed_spec["config"]["effective_marker"] == "resolved"
    async with AsyncSession(_engine) as db:
        row = await db.get(BenchmarkTrial, controller.owner.trial_id)
        assert row.snapshot == {"original": True}
        commands = (
            await db.scalars(select(BenchmarkCommand).order_by(BenchmarkCommand.sent_at))
        ).all()
        assert [item.method for item in commands] == [
            "describe",
            "validate",
            "plan",
            "prepare",
            "drive",
            "capture",
            "evaluate",
            "cleanup",
            "shutdown",
        ]
        assert all(item.state == "completed" for item in commands)
    with pytest.raises(ValueError, match="restart"):
        await controller.run(spec)


@pytest.mark.parametrize("phase", ["validate", "plan", "prepare", "drive", "capture", "evaluate"])
async def test_cancellation_is_supervised_at_each_worker_phase(driver, _engine, phase):
    controller, spec = driver
    spec["config"] = {"wait_phase": phase}
    pending = asyncio.create_task(controller.run(spec))
    try:
        await wait_for_phase(controller.store, controller.owner.trial_id, phase, pending)
        assert await controller.store.request_cancel(WORKSPACE, controller.owner.trial_id)
        result = await asyncio.wait_for(pending, 12)
        assert result["phase"] == "finished" and result["cleanup"] == "complete"
        assert result["execution"] == (
            "success" if phase in {"capture", "evaluate"} else "cancelled"
        )
        assert result["evaluation"] != "completed"
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("phase", ["prepare", "drive", "evaluate", "cleanup"])
async def test_explicit_native_failure_does_not_rewrite_execution_or_cleanup(driver, phase):
    controller, spec = driver
    spec["config"] = {"fail_phase": phase}
    result = await controller.run(spec)
    assert result["phase"] == "finished"
    assert result["execution"] == ("failed" if phase in {"prepare", "drive"} else "success")
    assert result["evaluation"] == (
        "error" if phase == "evaluate" else "completed" if phase == "cleanup" else "unavailable"
    )
    assert result["cleanup"] == ("error" if phase == "cleanup" else "complete")


async def test_worker_death_uses_file_policy_and_keeps_recording_incomplete(driver):
    controller, spec = driver
    spec["config"] = {"die_phase": "drive"}
    result = await controller.run(spec)
    assert result["execution"] == "failed" and result["recording"] == "incomplete"
    assert result["phase"] == "finished"


async def test_trial_deadline_cannot_be_sealed_as_success(driver, _engine):
    original, spec = driver
    store = original.store
    trial = await store.create(
        WORKSPACE, snapshot={}, deadline_at=datetime.now(UTC) + timedelta(seconds=3)
    )
    owner = await store.claim(WORKSPACE, trial.id, uuid.uuid4())
    controller = TrialDriver(
        Host(store, owner),
        RecoveryService(store, uuid.uuid4(), {}),
        capture_providers={},
        limits=original.limits,
    )
    spec["scope"]["trial_id"] = str(trial.id)
    spec["config"] = {"wait_phase": "drive"}
    pending = asyncio.create_task(controller.run(spec))
    try:
        result = await asyncio.wait_for(pending, 12)
        assert result["execution"] == "failed" and result["phase"] == "finished"
        async with AsyncSession(_engine) as db:
            assert (await db.get(BenchmarkTrial, owner.trial_id)).cancel_requested_at is None
            assert await db.scalar(
                select(BenchmarkJournalEntry.id).where(
                    BenchmarkJournalEntry.event == "controller.stopping",
                    BenchmarkJournalEntry.public_data["code"].astext == "TRIAL_DEADLINE",
                )
            )
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        if controller.host.client:
            await bounded(controller.host.client.close(0.2, shutdown=False), 3)


async def test_heartbeat_stays_live_while_native_phase_waits(driver, _engine):
    controller, spec = driver
    spec["config"] = {"wait_phase": "drive"}
    pending = asyncio.create_task(controller.run(spec))
    try:
        await wait_for_phase(controller.store, controller.owner.trial_id, "drive")
        async with AsyncSession(_engine) as db:
            initial = (await db.get(BenchmarkTrial, controller.owner.trial_id)).heartbeat_at
        async with asyncio.timeout(3):
            while True:
                async with AsyncSession(_engine) as db:
                    row = await db.get(BenchmarkTrial, controller.owner.trial_id)
                    if row.heartbeat_at > initial:
                        assert row.controller_expires_at > row.heartbeat_at
                        break
                await asyncio.sleep(0.1)
        await controller.store.request_cancel(WORKSPACE, controller.owner.trial_id)
        assert (await asyncio.wait_for(pending, 12))["execution"] == "cancelled"
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_live_driver_detaches_after_another_controller_claims(driver, _engine):
    controller, spec = driver
    spec["config"] = {"wait_phase": "drive"}
    pending = asyncio.create_task(controller.run(spec))
    try:
        await wait_for_phase(controller.store, controller.owner.trial_id, "drive")
        async with _engine.begin() as connection:
            await connection.execute(
                update(BenchmarkTrial)
                .where(BenchmarkTrial.id == controller.owner.trial_id)
                .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        claimed = await controller.store.claim(WORKSPACE, controller.owner.trial_id, uuid.uuid4())
        async with AsyncSession(_engine) as db:
            next_seq = (await db.get(BenchmarkTrial, claimed.trial_id)).next_seq
        result = await asyncio.wait_for(pending, 8)
        assert result["controller"] == "detached"
        async with AsyncSession(_engine) as db:
            row = await db.get(BenchmarkTrial, claimed.trial_id)
            assert row.generation == claimed.generation and row.execution_status == "lost"
            assert row.next_seq == next_seq
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize(
    "phase",
    ["created", "planning", "preparing", "ready", "driving", "capturing", "evaluating", "cleaning"],
)
async def test_restart_at_each_phase_recovers_without_starting_a_worker(driver, _engine, phase):
    original, spec = driver
    store, owner = original.store, original.owner
    phases = [
        "created",
        "planning",
        "preparing",
        "ready",
        "driving",
        "capturing",
        "evaluating",
        "cleaning",
    ]

    async def measure():
        return HostCapacity(8192, 8192, 4, datetime.now(UTC).isoformat())

    for before, after in zip(phases, phases[1:], strict=False):
        if before == phase:
            break
        if before == "planning":
            await store.reserve(
                owner,
                demands=(ResourceDemand("worker", QUOTA, 256, 0.1, 10),),
                native_resources={},
                measure=measure,
                runtime_policy=POLICY,
                group_policy=GroupPolicy(),
            )
        if after == "capturing":
            await store.finish_execution(owner, "success")
        await store.transition(owner, before, after)
    async with _engine.begin() as connection:
        await connection.execute(
            update(BenchmarkTrial)
            .where(BenchmarkTrial.id == owner.trial_id)
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    claimed = await store.claim(WORKSPACE, owner.trial_id, uuid.uuid4())
    host = Host(store, claimed)
    recovered = TrialDriver(
        host, RecoveryService(store, uuid.uuid4(), {}), capture_providers={}, limits=original.limits
    )
    result = await recovered.run(spec)
    assert result["phase"] == "finished" and result["cleanup"] == "complete"
    assert result["recording"] == "incomplete" and not host.started
    assert result["execution"] == (
        "success" if phase in {"capturing", "evaluating", "cleaning"} else "lost"
    )


async def test_execution_success_rejects_cancel_that_arrives_before_its_commit(driver):
    controller, _ = driver
    store, owner = controller.store, controller.owner
    await store.transition(owner, "created", "planning")

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
    for before, after in [("planning", "preparing"), ("preparing", "ready"), ("ready", "driving")]:
        await store.transition(owner, before, after)
    await store.request_cancel(WORKSPACE, owner.trial_id)
    with pytest.raises(ProtocolError) as stopping:
        await store.finish_execution(owner, "success")
    assert stopping.value.code == "TRIAL_STOPPING"


async def test_invalid_capture_cannot_produce_a_correctness_verdict(driver):
    controller, spec = driver
    spec["config"] = {"malformed_capture": True}
    result = await controller.run(spec)
    assert result["execution"] == "success" and result["evaluation"] == "unavailable"
    assert result["recording"] == "incomplete" and result["cleanup"] == "complete"


async def test_caller_cancellation_finishes_bounded_cleanup_before_propagating(driver, _engine):
    controller, spec = driver
    spec["config"] = {"wait_phase": "drive"}
    pending = asyncio.create_task(controller.run(spec))
    await wait_for_phase(controller.store, controller.owner.trial_id, "drive")
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 12)
    async with AsyncSession(_engine) as db:
        trial = await db.get(BenchmarkTrial, controller.owner.trial_id)
        assert trial.execution_status == "cancelled" and trial.phase == "finished"


async def test_second_driver_cannot_clean_up_an_active_owner_session(driver):
    original, spec = driver
    spec["config"] = {"wait_phase": "drive"}
    pending = asyncio.create_task(original.run(spec))
    try:
        await wait_for_phase(original.store, original.owner.trial_id, "drive", pending)
        another = TrialDriver(
            Host(original.store, original.owner),
            original.recovery,
            capture_providers={},
            limits=original.limits,
        )
        rejected = await another.run(spec)
        assert rejected["controller"] == "detached" and not another.host.started
        assert (await original.store.control(original.owner)).phase == "driving"
        await original.store.request_cancel(WORKSPACE, original.owner.trial_id)
        assert (await asyncio.wait_for(pending, 12))["execution"] == "cancelled"
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_unavailable_capture_preserves_the_reservation_for_retry(driver, _engine):
    controller, spec = driver
    spec["config"] = {"fail_phase": "capture"}
    controller.recovery.capture_files = AsyncMock(
        return_value={
            "complete": False,
            "artifacts": [],
            "errors": [{"code": "ARTIFACT_STORE_UNAVAILABLE"}],
        }
    )
    controller.recovery.sweep = AsyncMock(wraps=controller.recovery.sweep)
    result = await controller.run(spec)
    assert result["phase"] == "cleaning" and result["cleanup"] == "pending"
    assert result["execution"] == "success" and result["recording"] == "incomplete"
    assert all(call.args[1] != "release" for call in controller.recovery.sweep.call_args_list)
    async with AsyncSession(_engine) as db:
        assert await db.get(BenchmarkHostReservation, controller.store.host_id) is not None
