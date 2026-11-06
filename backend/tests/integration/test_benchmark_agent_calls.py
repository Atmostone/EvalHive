"""Agent retries, native evidence and controller fencing on real PostgreSQL."""

import asyncio
import subprocess
import sys
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.benchmarks.agent_calls import AgentCallStore
from app.benchmarks.harness_outcome import HarnessExecutionOutcome
from app.models.benchmark_agent_call import BenchmarkAgentCall
from app.models.benchmark_trial import (
    BenchmarkEventStream,
    BenchmarkHostReservation,
    BenchmarkResourceLease,
    BenchmarkTrial,
)
from app.models.execution_attempt import ExecutionAttempt, ExecutionCommand, ExecutionEvent
from app.models.task import Task
from evalhive_benchmark import ProtocolError, command
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from tests.integration.test_benchmark_trials import (
    WORKSPACE,
    advance_to_driving,
    fixture,  # noqa: F401
)


@pytest_asyncio.fixture
async def calls(fixture):  # noqa: F811
    store, owner, _, actor, _, _, _ = fixture
    async with AsyncSession(store.engine) as db:
        task = Task(title="Agent bridge fixture", workspace_id=WORKSPACE)
        db.add(task)
        await db.flush()
        await db.execute(
            update(BenchmarkTrial)
            .where(BenchmarkTrial.id == owner.trial_id)
            .values(task_id=task.id)
        )
        await db.commit()
    await store.register_stream(owner, actor)
    await advance_to_driving(store, owner)
    return AgentCallStore(store, owner, actor)


def frame(calls, operation="open", *, request_id="open-agent", payload=None, session_id=None):
    params = {
        "scope": {
            "workspace_id": str(calls.owner.workspace_id),
            "trial_id": str(calls.owner.trial_id),
            "generation": calls.owner.generation,
        },
        "operation": operation,
        "payload": {} if payload is None else payload,
    }
    if operation not in {"run", "open"}:
        params["session_id"] = session_id or str(calls.session_id)
    return command(request_id, "agent.call", params)


def receipt(request_id, result=None, *, error=None):
    return {
        "protocol_version": "1.0",
        "type": "response",
        "id": request_id,
        "ok": error is None,
        "result" if error is None else "error": error or result,
    }


def opened(calls):
    return {"session_id": str(calls.session_id), "attempt_id": str(calls.session_id)}


async def create_attempt(calls, *, binding=None):
    async with AsyncSession(calls.trials.engine) as db:
        trial = await db.get(BenchmarkTrial, calls.owner.trial_id)
        attempt = ExecutionAttempt(
            id=calls.session_id,
            task_id=trial.task_id,
            workspace_id=trial.workspace_id,
            number=1,
            connector_id="fixture.harness",
            connector_version="1.0",
            protocol_version="1.0",
            manifest={},
            run_spec={
                "metadata": {"benchmark_binding": binding or calls._binding(trial).metadata()}
            },
        )
        db.add(attempt)
        await db.execute(
            update(Task).where(Task.id == trial.task_id).values(current_attempt_id=attempt.id)
        )
        await db.commit()


async def start(calls, *, operation="open"):
    request = frame(calls, operation)
    ticket = await calls.begin(request)
    await create_attempt(calls)
    await calls.bind_attempt(ticket.id)
    if operation == "open":
        await calls.complete(ticket.id, receipt(request["id"], opened(calls)))
    return ticket


async def finish(calls, status="success"):
    async with AsyncSession(calls.trials.engine, expire_on_commit=False) as db:
        trial = await db.get(BenchmarkTrial, calls.owner.trial_id)
        attempt = await db.get(ExecutionAttempt, calls.session_id)
        attempt.status = status
        attempt.finished_at = datetime.now(UTC).replace(tzinfo=None)
        attempt.result = {
            "status": "failed" if status == "lost" else status,
            "summary": "Native completion",
        }
        if status in {"lost", "failed"}:
            attempt.result["error"] = {"code": "FIXTURE_FAILED", "message": "Native failure"}
        attempt.completeness = {
            "event_delivery": "interrupted" if status == "lost" else "contiguous"
        }
        outcome = HarnessExecutionOutcome.from_attempt(attempt, calls._binding(trial))
        await db.commit()
        return {
            "session_id": str(calls.session_id),
            "status": status,
            "outcome": outcome.reference(),
        }


async def test_intent_precedes_effect_and_completed_receipt_replays_without_execution(calls):
    request = frame(
        calls, payload={"initial_history": [{"role": "user", "text": "Private fixture"}]}
    )
    ticket = await calls.begin(request)
    async with AsyncSession(calls.trials.engine) as db:
        row = await db.scalar(
            select(BenchmarkAgentCall).options(undefer(BenchmarkAgentCall.private_request))
        )
        assert row.state == "started" and row.attempt_id is None
        assert b"Private fixture" not in row.private_request
    with pytest.raises(ProtocolError, match="cannot be executed again"):
        await calls.begin(request)
    await create_attempt(calls)
    await calls.bind_attempt(ticket.id)
    wire = receipt(request["id"], opened(calls))
    await calls.complete(ticket.id, wire)
    await calls.complete(ticket.id, wire)
    assert (await calls.begin(request)).replay == wire
    assert (await calls.begin(request, replay_only=True)).replay == wire
    async with AsyncSession(calls.trials.engine) as db:
        row = await db.scalar(
            select(BenchmarkAgentCall).options(undefer(BenchmarkAgentCall.private_response))
        )
        assert b"session_id" not in row.private_response


async def test_only_one_agent_start_and_no_reused_request_id(calls):
    await calls.begin(frame(calls))
    with pytest.raises(ProtocolError) as conflict:
        await calls.begin(frame(calls, "run"))
    assert conflict.value.code == "AGENT_CALL_CONFLICT"
    with pytest.raises(ProtocolError) as repeated:
        await calls.begin(frame(calls, request_id="another-start"))
    assert repeated.value.code == "AGENT_SESSION_EXISTS"
    with pytest.raises(ProtocolError) as absent:
        await calls.begin(frame(calls, request_id="retired-missing"), replay_only=True)
    assert absent.value.code == "RECEIPT_UNAVAILABLE"


async def test_concurrent_new_starts_serialize_before_any_effect(calls):
    results = await asyncio.gather(
        calls.begin(frame(calls)),
        calls.begin(frame(calls, request_id="concurrent")),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert [r.code for r in results if isinstance(r, ProtocolError)] == ["AGENT_SESSION_EXISTS"]


@pytest.mark.parametrize("status", ["success", "failed", "cancelled", "lost"])
async def test_completion_and_drive_reference_require_bound_committed_native_outcome(calls, status):
    ticket = await start(calls, operation="run")
    with pytest.raises(ProtocolError) as running:
        await calls.complete(
            ticket.id,
            receipt(
                "open-agent",
                {
                    "session_id": str(calls.session_id),
                    "status": "success",
                    "outcome": {"attempt_id": str(calls.session_id), "evidence_digest": "a" * 64},
                },
            ),
        )
    assert running.value.code == "HARNESS_RUNNING"
    result = await finish(calls, status)
    wrong = {**result, "status": "failed" if status == "success" else "success"}
    with pytest.raises(ProtocolError) as mismatch:
        await calls.complete(ticket.id, receipt("open-agent", wrong))
    assert mismatch.value.code == "HARNESS_RESULT_CONFLICT"
    await calls.complete(ticket.id, receipt("open-agent", result))
    assert (await calls.outcome(result["outcome"])).status == status
    with pytest.raises(ProtocolError):
        await calls.outcome({"finished": True})


async def test_binding_rejects_foreign_attempt_and_starting_without_current_attempt(calls):
    ticket = await calls.begin(frame(calls))
    await create_attempt(calls, binding={"trial_id": str(uuid.uuid4())})
    with pytest.raises(ProtocolError) as mismatch:
        await calls.bind_attempt(ticket.id)
    assert mismatch.value.code == "FOREIGN_HARNESS_ATTEMPT"


async def test_unbound_or_foreign_session_cannot_receive_commands(calls):
    await calls.begin(frame(calls))
    with pytest.raises(ProtocolError) as missing:
        await calls.begin(frame(calls, "close", request_id="close"))
    assert missing.value.code == "AGENT_START_UNCERTAIN"
    with pytest.raises(ProtocolError) as foreign:
        await calls.begin(frame(calls, "close", request_id="foreign", session_id=str(uuid.uuid4())))
    assert foreign.value.code == "FOREIGN_AGENT_SESSION"


async def test_acknowledged_control_must_match_native_command_and_payload(calls):
    await start(calls)
    payload = {"name": "turn.submit", "payload": {"content": "Fixture message"}}
    ticket = await calls.begin(frame(calls, "control", request_id="turn", payload=payload))
    command_id = uuid.uuid4()
    ack = receipt(str(command_id), {"accepted": True})
    result = {"session_id": str(calls.session_id), "command_id": str(command_id), "result": ack}
    with pytest.raises(ProtocolError) as missing:
        await calls.complete(ticket.id, receipt("turn", result))
    assert missing.value.code == "AGENT_COMMAND_CONFLICT"
    async with AsyncSession(calls.trials.engine) as db:
        db.add(
            ExecutionCommand(
                id=command_id,
                attempt_id=calls.session_id,
                name=payload["name"],
                payload=payload["payload"],
                status="acknowledged",
                result=ack,
            )
        )
        await db.commit()
    await calls.complete(ticket.id, receipt("turn", result))


async def test_events_page_has_exact_committed_payload_and_cursor(calls):
    await start(calls)
    native = {"seq": 0, "event": "message", "data": {"text": "Committed native output"}}
    async with AsyncSession(calls.trials.engine) as db:
        db.add(
            ExecutionEvent(
                attempt_id=calls.session_id,
                event_key="a" * 64,
                seq=0,
                event="message",
                payload=native,
            )
        )
        await db.execute(
            update(ExecutionAttempt)
            .where(ExecutionAttempt.id == calls.session_id)
            .values(next_seq=1)
        )
        await db.commit()
    ticket = await calls.begin(
        frame(calls, "events", request_id="page", payload={"cursor": 0, "limit": 1})
    )
    result = {"session_id": str(calls.session_id), "events": [native], "cursor": 1, "closed": False}
    with pytest.raises(ProtocolError):
        await calls.complete(ticket.id, receipt("page", {**result, "events": []}))
    with pytest.raises(ProtocolError):
        await calls.complete(ticket.id, receipt("page", {**result, "cursor": 2}))
    await calls.complete(ticket.id, receipt("page", result))


async def test_uncertain_calls_cannot_be_completed_by_a_late_reply(calls):
    ticket = await start(calls, operation="run")
    await calls.uncertain(ticket.id)
    result = await finish(calls)
    with pytest.raises(ProtocolError) as late:
        await calls.complete(ticket.id, receipt("open-agent", result))
    assert late.value.code == "AGENT_CALL_UNCERTAIN"
    async with AsyncSession(calls.trials.engine) as db:
        assert (await db.get(BenchmarkTrial, calls.owner.trial_id)).recording_status == "incomplete"


@pytest.mark.parametrize(
    "change,code",
    [
        ("cancel", "TRIAL_STOPPING"),
        ("deadline", "TRIAL_STOPPING"),
        ("phase", "INVALID_PHASE"),
        ("reservation", "NO_RESERVATION"),
        ("planning", "GROUP_ADMISSION_REQUIRED"),
        ("stream", "STREAM_CLOSED"),
    ],
)
async def test_host_checks_phase_admission_deadline_and_cancellation_before_intent(
    calls, change, code, monkeypatch
):
    async with AsyncSession(calls.trials.engine) as db:
        trial = await db.get(BenchmarkTrial, calls.owner.trial_id)
        if change == "cancel":
            trial.cancel_requested_at = datetime.now(UTC)
        elif change == "deadline":
            current = calls.trials.current

            async def after_deadline(db, owner, **kwargs):
                record, _ = await current(db, owner, **kwargs)
                return record, record.deadline_at + timedelta(seconds=1)

            monkeypatch.setattr(calls.trials, "current", after_deadline)
        elif change == "phase":
            trial.phase = "capturing"
        elif change == "reservation":
            await db.delete(await db.get(BenchmarkHostReservation, calls.trials.host_id))
        elif change == "planning":
            reservation = await db.get(BenchmarkHostReservation, calls.trials.host_id)
            reservation.budget = {**reservation.budget, "stage": "planning"}
        else:
            await db.execute(update(BenchmarkEventStream).values(final_seq=-1))
        await db.commit()
    with pytest.raises(ProtocolError) as error:
        await calls.begin(frame(calls))
    assert error.value.code == code
    async with AsyncSession(calls.trials.engine) as db:
        assert await db.scalar(select(BenchmarkAgentCall.id)) is None


async def test_stale_generation_cannot_receive_or_replay_a_previous_receipt(calls):
    await start(calls)
    async with AsyncSession(calls.trials.engine) as db:
        await db.execute(
            update(BenchmarkTrial).values(
                generation=calls.owner.generation + 1, controller_id=uuid.uuid4()
            )
        )
        await db.commit()
    with pytest.raises(ProtocolError) as fenced:
        await calls.begin(frame(calls), replay_only=True)
    assert fenced.value.code == "STALE_GENERATION"
    with pytest.raises(ValueError, match="Evaluation controllers"):
        AgentCallStore(calls.trials, replace(calls.owner, control_id=uuid.uuid4()), calls.actor_id)


async def test_receipt_and_intent_are_immutable_in_the_database(calls):
    await start(calls)
    async with AsyncSession(calls.trials.engine) as db:
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(update(BenchmarkAgentCall).values(private_response=b"replacement"))
        await db.rollback()
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(update(BenchmarkAgentCall).values(request_id="replacement"))
        await db.rollback()
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "downgrade", "bg11"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0 and "Finish benchmark agent sessions" in result.stderr
    async with calls.trials.engine.connect() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "bg12"


@pytest.mark.parametrize("bound", [False, True])
async def test_cleanup_keeps_reservation_until_created_agent_attempt_is_reconciled(calls, bound):
    ticket = await calls.begin(frame(calls))
    await create_attempt(calls)
    if bound:
        await calls.bind_attempt(ticket.id)
    async with AsyncSession(calls.trials.engine) as db:
        await db.execute(update(BenchmarkResourceLease).values(state="released"))
        await db.commit()
    await calls.trials.transition(calls.owner, "driving", "cleaning")
    await calls.trials.seal_stream(calls.owner, calls.actor_id, -1)
    with pytest.raises(ProtocolError) as active:
        await calls.trials.transition(calls.owner, "cleaning", "finished")
    assert active.value.code == "CLEANUP_PENDING"
    async with AsyncSession(calls.trials.engine) as db:
        assert await db.get(BenchmarkHostReservation, calls.trials.host_id) is not None
    await finish(calls, "lost")
    await calls.trials.transition(calls.owner, "cleaning", "finished")
    async with AsyncSession(calls.trials.engine) as db:
        assert await db.get(BenchmarkHostReservation, calls.trials.host_id) is None
        assert (await db.get(BenchmarkTrial, calls.owner.trial_id)).recording_status == "incomplete"


async def test_event_page_observed_before_terminal_remains_valid_after_terminal_commit(calls):
    await start(calls)
    ticket = await calls.begin(
        frame(calls, "events", request_id="page-before-terminal", payload={"cursor": 0})
    )
    page = {"session_id": str(calls.session_id), "events": [], "cursor": 0, "closed": False}
    await finish(calls)
    await calls.complete(ticket.id, receipt("page-before-terminal", page))
