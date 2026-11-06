"""One stream/receipt implementation, independent source and evaluator ownership."""

import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.benchmarks.commands import CommandStore
from app.benchmarks.worker_channel import WorkerChannel
from app.models.benchmark_command import BenchmarkCommand
from app.models.benchmark_evaluation_control import (
    BenchmarkEvaluationControl,
    BenchmarkEvaluationJournal,
)
from app.models.benchmark_trial import BenchmarkEventStream, BenchmarkJournalEntry, BenchmarkTrial
from evalhive_benchmark import ConnectorClient, ProtocolError, command
from evalhive_benchmark.protocol import response
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from tests.fixtures.benchmark_driver_worker import MANIFEST
from tests.integration.test_benchmark_trials import WORKSPACE
from tests.integration.test_evaluation_controls import controls as _controls
from tests.integration.test_evaluation_controls import driver as _driver

controls, driver = _controls, _driver


def event(owner, sequence=0, **changes):
    return {
        "protocol_version": "1.0",
        "type": "event",
        "id": "shared-event",
        "scope": {
            "workspace_id": str(owner.workspace_id),
            "trial_id": str(owner.trial_id),
            "generation": owner.generation,
        },
        "seq": sequence,
        "source": "native-grader",
        "event": "native",
        "data": {"private": "saved-evaluator-data"},
        **changes,
    }


async def setup_stream(controls):
    controller, store, source, owner, _ = controls
    await store.start_planning(owner)
    actor = controller.host.channel.actor_id
    stream = await store.register_stream(owner, actor)
    return controller, store, source, owner, actor, stream


async def test_events_deduplicate_and_use_evaluation_journal_only(controls):
    controller, store, source, owner, actor, stream = await setup_stream(controls)
    async with AsyncSession(store.engine) as db:
        before = (await db.get(BenchmarkTrial, source.trial_id)).next_seq
    frame = event(owner)
    assert await store.ingest(owner, actor, [frame]) == {"accepted_through": 0}
    assert await store.ingest(owner, actor, [frame]) == {"accepted_through": 0}
    with pytest.raises(ProtocolError) as conflict:
        await store.ingest(owner, actor, [{**frame, "data": {"changed": True}}])
    assert conflict.value.code == "EVENT_CONFLICT"
    with pytest.raises(ProtocolError) as gap:
        await store.ingest(owner, actor, [event(owner, 2, id="gap")])
    assert gap.value.code == "EVENT_GAP"
    await store.checkpoint_stream(owner, actor, 0)
    await store.seal_stream(owner, actor, 0)
    with pytest.raises(ProtocolError) as sealed:
        await store.ingest(owner, actor, [event(owner, 1, id="late")])
    assert sealed.value.code == "STREAM_SEALED"
    async with AsyncSession(store.engine) as db:
        rows = (
            await db.scalars(
                select(BenchmarkEvaluationJournal)
                .where(BenchmarkEvaluationJournal.stream_id == stream)
                .options(undefer(BenchmarkEvaluationJournal.private_data))
            )
        ).all()
        assert len(rows) == 1 and b"saved-evaluator-data" not in rows[0].private_data
        assert (await db.get(BenchmarkTrial, source.trial_id)).next_seq == before


async def test_command_identity_reuses_neither_source_stream_nor_receipt(controls):
    _, store, source, owner, actor, _ = await setup_stream(controls)
    async with AsyncSession(store.engine) as db:
        old = await db.scalar(
            select(BenchmarkCommand)
            .where(
                BenchmarkCommand.trial_id == source.trial_id, BenchmarkCommand.method == "describe"
            )
            .options(undefer(BenchmarkCommand.private_request))
        )
        frame = store.private.open("command-request:" + str(old.id), old.private_request)
        old_id = old.id
    ledger = CommandStore(store, owner, actor)
    ticket = await ledger.begin(frame)
    assert ticket.id != old_id and ticket.replay is None
    reply = response(frame["id"], {**MANIFEST, "display_name": "New evaluator manifest"})
    await ledger.receive(ticket.id, reply)
    await ledger.complete(ticket.id, event_floor=-1)
    await ledger.acknowledge(ticket.id)
    assert (await ledger.begin(frame)).replay == reply
    with pytest.raises(ProtocolError) as conflict:
        await ledger.begin(command(frame["id"], "shutdown"))
    assert conflict.value.code == "COMMAND_CONFLICT"


async def test_uncertain_commands_mark_only_evaluator_recording(controls):
    _, store, source, owner, actor, _ = await setup_stream(controls)
    ledger = CommandStore(store, owner, actor)
    ticket = await ledger.begin(command("pending", "describe"))
    await ledger.uncertain(ticket.id)
    with pytest.raises(ProtocolError) as uncertain:
        await ledger.begin(command("pending", "describe"))
    assert uncertain.value.code == "COMMAND_UNCERTAIN"
    async with AsyncSession(store.engine) as db:
        assert (
            await db.get(BenchmarkEvaluationControl, owner.control_id)
        ).recording_status == "incomplete"
        assert (await db.get(BenchmarkTrial, source.trial_id)).recording_status == "complete"


async def test_takeover_rejects_late_events_and_responses(controls):
    _, store, source, owner, actor, _ = await setup_stream(controls)
    ledger = CommandStore(store, owner, actor)
    ticket = await ledger.begin(command("pending", "describe"))
    async with store.engine.begin() as db:
        await db.execute(
            update(BenchmarkEvaluationControl)
            .where(BenchmarkEvaluationControl.id == owner.control_id)
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    claimed = await store.claim(WORKSPACE, source.trial_id, owner.control_id, uuid.uuid4())
    for operation in (
        store.ingest(owner, actor, [event(owner)]),
        ledger.receive(ticket.id, response("pending", MANIFEST)),
    ):
        with pytest.raises(ProtocolError) as stale:
            await operation
        assert stale.value.code == "STALE_GENERATION"
    await store.register_stream(claimed, actor)
    assert await store.ingest(claimed, actor, [event(claimed)]) == {"accepted_through": 0}


@pytest.mark.parametrize("target", ["execution_journal", "evaluation_journal", "stream"])
async def test_database_rejects_cross_context_events_and_rebinding(controls, target):
    controller, store, source, owner, _, stream = await setup_stream(controls)
    async with AsyncSession(store.engine) as db:
        original = await db.scalar(
            select(BenchmarkEventStream.id).where(
                BenchmarkEventStream.control_id.is_(None),
                BenchmarkEventStream.actor_id == controller.host.channel.actor_id,
            )
        )
        with pytest.raises(DBAPIError, match="context differs|immutable"):
            if target == "execution_journal":
                db.add(
                    BenchmarkJournalEntry(
                        trial_id=source.trial_id,
                        stream_id=stream,
                        seq=9999,
                        generation=1,
                        event="native",
                        public_data={},
                    )
                )
                await db.flush()
            elif target == "evaluation_journal":
                db.add(
                    BenchmarkEvaluationJournal(
                        control_id=owner.control_id,
                        stream_id=original,
                        seq=9999,
                        generation=1,
                        event="native",
                        public_data={},
                    )
                )
                await db.flush()
            else:
                await db.execute(
                    update(BenchmarkEventStream)
                    .where(BenchmarkEventStream.id == stream)
                    .values(control_id=None)
                )
        await db.rollback()


async def test_real_sdk_worker_channel_drains_into_evaluator_context(controls):
    controller, store, source, owner, _ = controls
    await store.start_planning(owner)
    worker = Path(__file__).parents[1] / "fixtures/benchmark_driver_worker.py"
    client = await ConnectorClient.launch(
        [sys.executable, str(worker)],
        env={"PATH": os.defpath, "PYTHONPATH": "/opt/current-benchmark-sdk"},
    )
    channel = await WorkerChannel.attach(store, owner, uuid.uuid4(), client)
    try:
        await channel.invoke("describe", request_id="describe")
        spec = controller.host.installed_spec
        assert (await channel.invoke("validate", {"spec": spec}, request_id="validate"))["accepted"]
        await channel.close(grace=1)
        async with AsyncSession(store.engine) as db:
            streams = (
                await db.scalars(
                    select(BenchmarkEventStream).where(
                        BenchmarkEventStream.control_id == owner.control_id
                    )
                )
            ).all()
            assert len(streams) == 1 and streams[0].final_seq == streams[0].next_seq - 1
            rows = (
                await db.scalars(
                    select(BenchmarkCommand).where(BenchmarkCommand.stream_id == streams[0].id)
                )
            ).all()
            assert {row.method for row in rows} == {"describe", "validate", "shutdown"}
            assert all(row.state == "completed" for row in rows)
            assert (await db.get(BenchmarkTrial, source.trial_id)).phase == "finished"
    finally:
        await asyncio.wait_for(client.close(0.2, shutdown=False), 3)
