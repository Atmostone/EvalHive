"""Real SDK subprocess + PostgreSQL: persistence before barriers and ACK."""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from app.benchmarks.worker_channel import WorkerChannel
from app.models.benchmark_command import BenchmarkCommand
from app.models.benchmark_trial import BenchmarkEventStream, BenchmarkJournalEntry, BenchmarkTrial
from evalhive_benchmark import ConnectorClient, ProtocolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.test_benchmark_trials import advance_to_driving, fixture, request  # noqa: F401


@pytest_asyncio.fixture
async def channel(fixture):  # noqa: F811
    store, owner, _, actor, _, _, _ = fixture
    await advance_to_driving(store, owner)
    worker = Path(__file__).parents[1] / "fixtures/benchmark_channel_worker.py"
    client = await ConnectorClient.launch(
        [sys.executable, str(worker)],
        env={"PATH": os.defpath, "PYTHONPATH": "/opt/current-benchmark-sdk"},
    )
    bound = await WorkerChannel.attach(store, owner, actor, client)
    try:
        yield bound
    finally:
        try:
            await asyncio.wait_for(bound.close(grace=1), 8)
        except ProtocolError:
            pass  # Recording-error tests deliberately leave an incomplete stream.


def params(channel, payload=None):
    owner = channel.owner
    return {
        "scope": {
            "workspace_id": str(owner.workspace_id),
            "trial_id": str(owner.trial_id),
            "generation": owner.generation,
        },
        "payload": payload or {},
    }


async def test_channel_persists_ordered_events_before_ack_and_replays_without_send(
    channel, _engine
):
    original = channel.client.request_receipt
    channel.client.request_receipt = AsyncMock(wraps=original)
    first, duplicate = await asyncio.gather(
        channel.invoke("drive", params(channel), request_id="same-drive"),
        channel.invoke("drive", params(channel), request_id="same-drive"),
    )
    assert first == duplicate == {"finished": True, "calls": 1}
    assert await channel.invoke("drive", params(channel), request_id="same-drive") == first
    assert channel.client.request_receipt.call_count == 1
    assert channel.client.events_received == channel.persisted == 2
    async with AsyncSession(_engine) as db:
        row = await db.scalar(select(BenchmarkCommand))
        assert (
            row.state == "completed"
            and row.acknowledged_at is not None
            and row.event_through_seq == 1
        )
        entries = (
            await db.scalars(select(BenchmarkJournalEntry).order_by(BenchmarkJournalEntry.seq))
        ).all()
        names = [entry.event for entry in entries]
        assert names.index("command.completed") > max(
            index for index, name in enumerate(names) if name == "native"
        )
        assert names.index("command.acknowledged") > names.index("command.completed")
        assert "private-fixture-observation" not in str([entry.public_data for entry in entries])
    await channel.trials.finish_execution(channel.owner, "success")
    await channel.trials.transition(channel.owner, "driving", "capturing")
    await channel.invoke("capture", params(channel))
    assert channel.event_floor == 2


async def test_cancel_remains_responsive_while_native_resource_fence_is_held(channel, fixture):  # noqa: F811
    _, _, scope, _, _, authority, _ = fixture
    pending = asyncio.create_task(channel.invoke("drive", params(channel, {"wait": True})))
    while channel.persisted < 1:
        await asyncio.sleep(0.01)
    async with authority.operation(request(scope)) as resource:
        assert (await asyncio.wait_for(channel.invoke("cancel", params(channel)), 2))["cancelled"]
        assert (await asyncio.wait_for(pending, 2))["cancelled"]
        await resource.complete({"checked": True})


async def test_wire_error_is_saved_acknowledged_and_replayed_without_execution(channel, _engine):
    for _ in range(2):
        with pytest.raises(ProtocolError) as failed:
            await channel.invoke(
                "drive", params(channel, {"fail": True}), request_id="rejected-drive"
            )
        assert failed.value.code == "CONNECTOR_COMMAND_FAILED"
    async with AsyncSession(_engine) as db:
        row = await db.scalar(select(BenchmarkCommand))
        assert row.state == "completed" and row.acknowledged_at is not None


@pytest.mark.parametrize("payload", [{"foreign": True}, {"die": True}])
async def test_recording_or_transport_loss_cannot_complete_command(channel, _engine, payload):
    with pytest.raises(ProtocolError):
        await asyncio.wait_for(
            channel.invoke("drive", params(channel, payload), request_id="lost-drive"), 5
        )
    async with AsyncSession(_engine) as db:
        row = await db.scalar(select(BenchmarkCommand))
        assert row.state == "uncertain" and row.acknowledged_at is None
        assert (
            await db.get(BenchmarkTrial, channel.owner.trial_id)
        ).recording_status == "incomplete"


async def test_closing_channel_seals_only_after_final_shutdown_receipt(channel, _engine):
    await channel.invoke("drive", params(channel))
    await channel.close(grace=1)
    async with AsyncSession(_engine) as db:
        stream = await db.scalar(select(BenchmarkEventStream))
        assert stream.final_seq == 1
        shutdown = await db.scalar(
            select(BenchmarkCommand).where(BenchmarkCommand.method == "shutdown")
        )
        assert shutdown.state == "completed" and shutdown.acknowledged_at is not None


async def test_response_is_persisted_while_event_delivery_blocks_ack(channel, _engine, monkeypatch):
    gate = asyncio.Event()
    original = channel.trials.ingest

    async def delayed(*args, **kwargs):
        await gate.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(channel.trials, "ingest", delayed)
    pending = asyncio.create_task(
        channel.invoke("drive", params(channel), request_id="delayed-events")
    )
    try:
        for _ in range(100):
            async with AsyncSession(_engine) as db:
                row = await db.scalar(
                    select(BenchmarkCommand).where(BenchmarkCommand.request_id == "delayed-events")
                )
                if row is not None and row.state == "received":
                    assert row.acknowledged_at is None and row.completed_at is None
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Wire receipt was not persisted independently of event delivery")
        assert not pending.done() and channel.client.events_received == 2 and channel.persisted == 0
        gate.set()
        assert (await asyncio.wait_for(pending, 5))["finished"]
    finally:
        gate.set()
        await asyncio.gather(pending, return_exceptions=True)
