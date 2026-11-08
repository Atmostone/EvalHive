"""The shared Harness runner commits into a fenced trial without settling its task."""

# ruff: noqa: F811

import asyncio
from copy import deepcopy

import pytest
from app.benchmarks.harness_api import BenchmarkAttemptAPI
from app.harnesses.attempts import queue_command
from app.harnesses.catalog import get_manifest
from app.models.benchmark_trial import BenchmarkTrial
from app.models.execution_attempt import ExecutionAttempt, ExecutionEvent
from app.models.task import Task
from evalhive_benchmark import ProtocolError
from evalhive_harness import response
from evalhive_runner.runner import AttemptRunner, RunnerPolicy
from evalhive_runner.spool import EventSpool
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.test_benchmark_agent_calls import (
    calls,  # noqa: F401
    create_attempt,
    fixture,  # noqa: F401
    frame,
    start,
)


class Connector:
    def __init__(self, manifest, spec, *, wait=False):
        self.manifest, self.spec = manifest, spec
        self.started, self.finish = asyncio.Event(), asyncio.Event()
        self.closed = False
        self.status = "success"
        if not wait:
            self.finish.set()

    async def request(self, method, params=None, **kwargs):
        if method == "describe":
            return self.manifest
        if method == "validate":
            return {"accepted": True, "effective_config": params["run"]["config"]}
        if method == "start":
            self.started.set()
        if method == "control" and params["command"] == "abort":
            self.status = "cancelled"
            self.finish.set()
        return {"accepted": True}

    async def events(self):
        await self.started.wait()
        yield self.event(0, "run.started", {})
        await self.finish.wait()
        yield self.event(
            1, "run.finished", {"status": self.status, "summary": "Fixture completion"}
        )

    def event(self, seq, name, data):
        return {
            "protocol_version": "1.0",
            "type": "event",
            "attempt_id": self.spec["attempt_id"],
            "event_id": "native-" + str(seq),
            "seq": seq,
            "source": "fixture",
            "event": name,
            "data": data,
        }

    async def close(self, **kwargs):
        self.closed = True


async def configured(calls):  # noqa: F811
    await start(calls)
    async with AsyncSession(calls.trials.engine, expire_on_commit=False) as db:
        attempt = await db.get(ExecutionAttempt, calls.session_id)
        manifest = get_manifest("pi")
        spec = {
            "protocol_version": "1.0",
            "workspace_id": str(attempt.workspace_id),
            "task_id": str(attempt.task_id),
            "attempt_id": str(attempt.id),
            "task": {"prompt": "Run an isolated engineering fixture"},
            "cwd": "/workspace",
            "config": {},
            "metadata": deepcopy(attempt.run_spec["metadata"]),
            "limits": {"wall_time_seconds": 20},
        }
        attempt.manifest, attempt.run_spec = manifest, spec
        attempt.connector_id, attempt.connector_version = manifest["id"], manifest["version"]
        task = await db.get(Task, attempt.task_id)
        task.status = "in_progress"
        await db.commit()
        return manifest, spec


POLICY = RunnerPolicy(
    heartbeat_seconds=0.03,
    retry_seconds=0.02,
    flush_seconds=5,
    shutdown_seconds=1,
    command_seconds=3,
    startup_seconds=3,
)


async def test_shared_attempt_runner_saves_final_events_and_does_not_finish_benchmark_task(
    calls, tmp_path
):  # noqa: F811
    manifest, spec = await configured(calls)
    api, connector = BenchmarkAttemptAPI(calls), Connector(manifest, spec)
    with EventSpool(tmp_path / "agent-spool.sqlite") as spool:
        runner = AttemptRunner(
            spec=spec, manifest=manifest, connector=connector, api=api, spool=spool, policy=POLICY
        )
        result = await asyncio.wait_for(runner.run(), 15)
        assert result == {"status": "success", "pending_events": 0}
    assert connector.closed
    async with AsyncSession(calls.trials.engine) as db:
        attempt = await db.get(ExecutionAttempt, calls.session_id)
        assert attempt.status == "success" and attempt.prepared_at is not None
        assert attempt.completeness["event_delivery"] == "contiguous"
        assert (await db.get(Task, attempt.task_id)).status == "in_progress"
        assert (await db.get(BenchmarkTrial, calls.owner.trial_id)).phase == "driving"
        assert list(
            await db.scalars(select(ExecutionEvent.event).order_by(ExecutionEvent.seq))
        ) == ["run.started", "run.finished"]


async def test_trial_cancellation_reaches_shared_runner_and_native_connector(calls, tmp_path):  # noqa: F811
    manifest, spec = await configured(calls)
    api, connector = BenchmarkAttemptAPI(calls), Connector(manifest, spec, wait=True)
    with EventSpool(tmp_path / "cancel-spool.sqlite") as spool:
        runner = AttemptRunner(
            spec=spec, manifest=manifest, connector=connector, api=api, spool=spool, policy=POLICY
        )
        running = asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(connector.started.wait(), 5)
            await calls.trials.request_stop(calls.owner)
            result = await asyncio.wait_for(running, 15)
            assert result["status"] == "cancelled" and connector.closed
        finally:
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
    async with AsyncSession(calls.trials.engine) as db:
        assert (await db.get(ExecutionAttempt, calls.session_id)).status == "cancelled"


async def test_unbound_startup_and_stale_controller_cannot_write_harness_journal(calls):  # noqa: F811
    await calls.begin(frame(calls))
    await create_attempt(calls)
    api = BenchmarkAttemptAPI(calls)
    with pytest.raises(ProtocolError) as unbound:
        await api.heartbeat()
    assert unbound.value.code == "AGENT_NOT_BOUND"
    async with AsyncSession(calls.trials.engine) as db:
        await db.execute(update(BenchmarkTrial).values(generation=calls.owner.generation + 1))
        await db.commit()
    with pytest.raises(ProtocolError) as stale:
        await api.events([])
    assert stale.value.code == "STALE_GENERATION"
    async with AsyncSession(calls.trials.engine) as db:
        assert (await db.get(ExecutionAttempt, calls.session_id)).next_seq == 0


async def test_shared_command_delivery_and_acknowledgements_are_idempotent(calls):  # noqa: F811
    await configured(calls)
    api = BenchmarkAttemptAPI(calls)
    async with AsyncSession(calls.trials.engine) as db:
        queued = await queue_command(db, calls.session_id, "abort", {})
        await db.commit()
    first, duplicate = await api.heartbeat(), await api.heartbeat()
    assert first == duplicate
    assert first["commands"] == [{"id": queued["id"], "name": "abort", "payload": {}}]
    wire = response(queued["id"], {"accepted": True})
    assert await api.acknowledge(queued["id"], wire) == {"duplicate": False}
    assert await api.acknowledge(queued["id"], wire) == {"duplicate": True}
    assert (await api.heartbeat())["commands"] == []


async def test_preparation_after_cancellation_is_rejected_before_configuration_changes(calls):  # noqa: F811
    await configured(calls)
    await calls.trials.request_stop(calls.owner)
    with pytest.raises(ProtocolError) as stopped:
        await BenchmarkAttemptAPI(calls).prepare({})
    assert stopped.value.code == "TRIAL_STOPPING"
    async with AsyncSession(calls.trials.engine) as db:
        assert (await db.get(ExecutionAttempt, calls.session_id)).prepared_at is None


async def test_new_heartbeat_reflects_queued_abort_during_cleaning(calls):  # noqa: F811
    await configured(calls)
    await calls.trials.transition(calls.owner, "driving", "cleaning")
    receipt = await BenchmarkAttemptAPI(calls).heartbeat()
    assert receipt["status"] == "stopping" and receipt["commands"][0]["name"] == "abort"
