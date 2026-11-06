"""Durable worker receipts, barriers and fencing on real PostgreSQL."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.benchmarks.commands import CommandStore
from app.models.benchmark_command import BenchmarkCommand
from app.models.benchmark_trial import BenchmarkEventStream, BenchmarkTrial
from evalhive_benchmark import ProtocolError, command
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from tests.integration.test_benchmark_trials import (
    MANIFEST,
    WORKSPACE,
    advance_to_driving,
    event,
    fixture,  # noqa: F401
    request,
)


@pytest_asyncio.fixture
async def journal(fixture):  # noqa: F811
    store, owner, _, actor, _, _, _ = fixture
    await store.register_stream(owner, actor)
    return CommandStore(store, owner, actor)


def receipt(request_id, result=None, *, error=None):
    value = {"protocol_version": "1.0", "type": "response", "id": request_id, "ok": error is None}
    value["result" if error is None else "error"] = (
        MANIFEST if result is None and error is None else error or result
    )
    return value


async def test_worker_command_intent_receipt_barrier_and_replay(journal, _engine):
    frame = command("stable-command", "describe")
    ticket = await journal.begin(frame)
    with pytest.raises(ProtocolError, match="cannot be sent again"):
        await journal.begin(frame)
    with pytest.raises(ProtocolError, match="ACK requires"):
        await journal.acknowledge(ticket.id)
    wire = receipt(frame["id"])
    await journal.receive(ticket.id, wire)
    await journal.receive(ticket.id, wire)
    with pytest.raises(ProtocolError, match="immutable"):
        await journal.receive(ticket.id, receipt(frame["id"], {}))
    final = await journal.complete(ticket.id, event_floor=-1)
    assert final.replay == wire
    await journal.acknowledge(ticket.id)
    assert (await journal.begin(frame)).replay == wire
    with pytest.raises(ProtocolError, match="different content"):
        await journal.begin(command(frame["id"], "shutdown"))
    async with AsyncSession(_engine) as db:
        row = await db.scalar(
            select(BenchmarkCommand).options(
                undefer(BenchmarkCommand.private_request),
                undefer(BenchmarkCommand.private_response),
            )
        )
        assert row.state == "completed" and row.acknowledged_at is not None
        assert row.event_through_seq == -1
        assert b"fixture.rootless" not in row.private_response


async def test_completion_waits_for_events_and_leaves_stream_open(journal, fixture, _engine):  # noqa: F811
    store, owner, _, actor, _, _, _ = fixture
    ticket = await journal.begin(command("with-events", "describe"))
    await journal.receive(ticket.id, receipt("with-events"))
    with pytest.raises(ProtocolError, match="preceding events"):
        await journal.complete(ticket.id, event_floor=0)
    await store.ingest(owner, actor, [event(owner, 0)])
    await journal.complete(ticket.id, event_floor=0)
    await store.ingest(owner, actor, [event(owner, 1, source="later-capture")])
    async with AsyncSession(_engine) as db:
        stream = await db.scalar(select(BenchmarkEventStream))
        assert stream.checkpoint_seq == 0 and stream.next_seq == 2 and stream.final_seq is None


async def test_command_journal_does_not_wait_for_native_resource_fence(journal, fixture):  # noqa: F811
    store, _, scope, _, _, authority, _ = fixture
    import asyncio

    async with authority.operation(request(scope)) as resource:
        ticket = await asyncio.wait_for(journal.begin(command("during-resource", "describe")), 2)
        await asyncio.wait_for(journal.receive(ticket.id, receipt("during-resource")), 2)
        await asyncio.wait_for(journal.complete(ticket.id, event_floor=-1), 2)
        await resource.complete({"checked": True})


async def test_interrupted_command_is_never_resent_and_keeps_unknown_outcome(
    journal,
    fixture,  # noqa: F811
    _engine,  # noqa: F811
):  # noqa: F811
    store, owner, _, _, _, _, _ = fixture
    frame = command("lost-response", "describe")
    ticket = await journal.begin(frame)
    await journal.uncertain(ticket.id, code="COMMAND_TIMEOUT")
    with pytest.raises(ProtocolError, match="cannot be sent again"):
        await journal.begin(frame)
    with pytest.raises(ProtocolError, match="Late response"):
        await journal.receive(ticket.id, receipt(frame["id"]))
    async with AsyncSession(_engine) as db:
        assert (await db.get(BenchmarkTrial, owner.trial_id)).recording_status == "incomplete"


@pytest.mark.parametrize("kind", ["invalid", "uncertain"])
async def test_invalid_or_uncertain_native_receipt_is_saved_and_classified(
    journal,
    fixture,  # noqa: F811
    _engine,
    kind,  # noqa: F811
):  # noqa: F811
    store, owner, _, _, _, _, _ = fixture
    ticket = await journal.begin(command("native-result", "describe"))
    wire = (
        receipt("native-result", {})
        if kind == "invalid"
        else receipt(
            "native-result",
            error={
                "code": "NATIVE_UNKNOWN",
                "kind": "uncertain",
                "message": "Private native detail",
            },
        )
    )
    await journal.receive(ticket.id, wire)
    completed = await journal.complete(ticket.id, event_floor=-1)
    assert completed.error_code == (
        "INVALID_CONNECTOR_RESULT" if kind == "invalid" else "CONNECTOR_OUTCOME_UNCERTAIN"
    )
    assert (await journal.begin(command("native-result", "describe"))).replay == wire
    await journal.acknowledge(ticket.id)
    async with AsyncSession(_engine) as db:
        assert (await db.get(BenchmarkTrial, owner.trial_id)).recording_status == "incomplete"


async def test_stale_controller_cannot_save_a_late_command_result(journal, fixture, _engine):  # noqa: F811
    store, owner, _, _, _, _, _ = fixture
    ticket = await journal.begin(command("old-controller", "describe"))
    async with _engine.begin() as connection:
        await connection.execute(
            update(BenchmarkTrial)
            .where(BenchmarkTrial.id == owner.trial_id)
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    await store.claim(WORKSPACE, owner.trial_id, uuid.uuid4())
    with pytest.raises(ProtocolError, match="no longer owns"):
        await journal.receive(ticket.id, receipt("old-controller"))


async def test_database_rejects_rewriting_command_intent_or_wire_receipt(journal, _engine):
    ticket = await journal.begin(command("immutable", "describe"))
    await journal.receive(ticket.id, receipt("immutable"))
    for values in [{"content_hash": "0" * 64}, {"private_response": b"rewritten"}]:
        with pytest.raises(DBAPIError, match="immutable"):
            async with _engine.begin() as connection:
                await connection.execute(
                    update(BenchmarkCommand)
                    .where(BenchmarkCommand.id == ticket.id)
                    .values(**values)
                )


async def test_execution_cannot_finish_with_an_open_drive_command(journal, fixture):  # noqa: F811
    store, owner, _, actor, _, _, _ = fixture
    await advance_to_driving(store, owner)
    params = {
        "scope": {
            "workspace_id": str(owner.workspace_id),
            "trial_id": str(owner.trial_id),
            "generation": owner.generation,
        },
        "payload": {},
    }
    ticket = await journal.begin(command("unfinished-drive", "drive", params))
    await store.checkpoint_stream(owner, actor, -1)
    with pytest.raises(ProtocolError, match="durable worker responses"):
        await store.finish_execution(owner, "success")
    await journal.receive(ticket.id, receipt("unfinished-drive", {"finished": True}))
    await journal.complete(ticket.id, event_floor=-1)
    await store.finish_execution(owner, "success")
