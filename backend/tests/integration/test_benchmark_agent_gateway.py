"""SDK wire, shared runner and durable host calls compose without external models."""

# ruff: noqa: F811

import asyncio
from types import SimpleNamespace

import pytest
from app.benchmarks.agent_gateway import AgentGateway
from app.benchmarks.harness_execution import HarnessExecution
from app.harnesses.catalog import get_manifest
from app.models.benchmark_agent_call import BenchmarkAgentCall
from app.models.benchmark_trial import BenchmarkTrial
from app.models.execution_attempt import ExecutionAttempt, ExecutionEvent
from evalhive_benchmark import ProtocolError
from evalhive_harness.connector import Connector, serve
from evalhive_runner.runner import RunnerPolicy
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.test_benchmark_agent_calls import (
    calls,  # noqa: F401
    fixture,  # noqa: F401
    frame,
    start,
)


class EngineeringConnector(Connector):
    manifest = get_manifest("pi")

    def __init__(self, *, wait=False, private=""):
        self.wait, self.private = wait, private

    async def run(self, context):
        if context.conversation is not None:
            while incoming := await context.conversation.next_input():
                text = incoming["messages"][0]["content"]
                if text == "tools":
                    results = await context.conversation.request_tools(
                        "tool-message",
                        "batch-1",
                        [
                            {"call_id": "tool-1", "name": "echo", "arguments": {"text": "one"}},
                        ],
                    )
                    text = str(results[0]["content"])
                await context.conversation.complete_turn(
                    "answer-" + incoming["turn_id"], "Echo: " + text
                )
            return {"status": "success", "summary": "Conversation closed"}
        if self.wait:
            await context.cancelled.wait()
        return {"status": "success", "summary": "Engineering result " + self.private}

    async def control(self, context, name, payload):
        if name == "abort":
            context.cancelled.set()
        return {"accepted": True}


class WireProcess:
    """In-memory process pipes around the real SDK serve() implementation."""

    def __init__(self, connector):
        self.incoming, self.outgoing = asyncio.Queue(128), asyncio.Queue(128)
        self.returncode = None
        self.stdin = SimpleNamespace(
            write=self.incoming.put_nowait,
            drain=self.drain,
            close=lambda: self.incoming.put_nowait(b""),
        )
        self.stdout = SimpleNamespace(readline=self.readline)
        self.job = asyncio.create_task(self.run(connector))

    async def drain(self):
        await asyncio.sleep(0)

    async def readline(self):
        return await self.outgoing.get()

    async def run(self, connector):
        async def lines():
            while line := await self.incoming.get():
                yield line

        try:
            await serve(connector, lines(), self.outgoing.put)
        except asyncio.CancelledError:
            pass
        finally:
            self.returncode = 0
            await self.outgoing.put(b"")

    async def wait(self):
        await asyncio.shield(self.job)
        return self.returncode

    def terminate(self):
        self.job.cancel()

    kill = terminate


POLICY = RunnerPolicy(
    heartbeat_seconds=0.03,
    retry_seconds=0.02,
    flush_seconds=3,
    shutdown_seconds=1,
    command_seconds=3,
    startup_seconds=3,
)


async def gateway_for(caller, tmp_path, *, mode="single_turn", wait=False, private=""):
    async with AsyncSession(caller.trials.engine) as db:
        trial = await db.get(BenchmarkTrial, caller.owner.trial_id)
        spec = {
            "protocol_version": "1.0",
            "workspace_id": str(trial.workspace_id),
            "task_id": str(trial.task_id),
            "attempt_id": str(caller.session_id),
            "task": {"prompt": "Isolated gateway fixture"},
            "cwd": "/workspace",
            "config": {},
            "interaction": {"version": "1.0", "mode": mode},
        }
    started = []

    async def transport():
        # The real provider factory will make this check under its resource fence.
        async with caller.runner_transaction():
            pass
        process = WireProcess(EngineeringConnector(wait=wait, private=private))
        started.append(process)
        return process

    execution = HarnessExecution(
        caller,
        get_manifest("pi"),
        spec,
        transport,
        tmp_path / "spool.sqlite",
        private_values=[private] if private else [],
        policy=POLICY,
    )
    return AgentGateway(caller, execution), started


async def test_autonomous_wire_run_returns_confirmed_outcome_and_retired_replay_has_no_effect(
    calls, tmp_path
):  # noqa: F811
    gateway, started = await gateway_for(calls, tmp_path, private="fixture-secret-never-public")
    request = frame(calls, "run", request_id="run")
    result = await asyncio.wait_for(gateway.handle(request), 15)
    async with AsyncSession(calls.trials.engine) as db:
        evidence = (await db.get(ExecutionAttempt, calls.session_id)).result
    assert result["status"] == "success" and len(started) == 1, evidence
    assert (await gateway.outcome(result["outcome"])).status == "success"
    assert await gateway.handle(request) == result
    assert await gateway.replay(request) == result
    assert len(started) == 1
    async with AsyncSession(calls.trials.engine) as db:
        attempt = await db.get(ExecutionAttempt, calls.session_id)
        assert attempt.result["summary"] == "Engineering result [REDACTED]"
        assert started[0].returncode == 0


async def test_conversation_open_control_events_and_close_use_the_same_sdk_session(calls, tmp_path):  # noqa: F811
    gateway, started = await gateway_for(calls, tmp_path, mode="conversation")
    opened = await asyncio.wait_for(gateway.handle(frame(calls)), 15)
    assert opened["session_id"] == str(calls.session_id)
    native_input = {
        "input_id": "native-input",
        "turn_id": "turn-1",
        "expected_revision": 0,
        "messages": [{"role": "user", "content": "Fixture question"}],
    }
    control = await asyncio.wait_for(
        gateway.handle(
            frame(
                calls,
                "control",
                request_id="turn",
                payload={"name": "turn.submit", "payload": native_input},
            )
        ),
        10,
    )
    assert control["result"]["ok"]
    cursor, events = 0, []
    async with asyncio.timeout(10):
        while not any(
            event["event"] == "session.waiting_input" and event["data"]["revision"] == 1
            for event in events
        ):
            page = await gateway.handle(
                frame(
                    calls,
                    "events",
                    request_id="page-" + str(len(events)),
                    payload={"cursor": cursor, "wait_seconds": 1},
                )
            )
            cursor = page["cursor"]
            events.extend(page["events"])
    closed = await asyncio.wait_for(gateway.handle(frame(calls, "close", request_id="close")), 15)
    assert closed["status"] == "success" and len(started) == 1


async def test_cancelling_gateway_await_does_not_repeat_the_native_run(calls, tmp_path):  # noqa: F811
    gateway, started = await gateway_for(calls, tmp_path, wait=True)
    request = frame(calls, "run", request_id="run-cancelled")
    running = asyncio.create_task(gateway.handle(request))
    try:
        async with asyncio.timeout(10):
            while True:
                async with AsyncSession(calls.trials.engine) as db:
                    attempt = await db.get(ExecutionAttempt, calls.session_id)
                    if attempt is not None and attempt.status == "running":
                        break
                await asyncio.sleep(0.05)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await gateway.cancel()
        await asyncio.wait_for(gateway.executor.wait(), 10)
        with pytest.raises(ProtocolError) as uncertain:
            await gateway.handle(request)
        assert uncertain.value.code == "AGENT_CALL_UNCERTAIN" and len(started) == 1
        async with AsyncSession(calls.trials.engine) as db:
            assert (await db.get(ExecutionAttempt, calls.session_id)).status == "cancelled"
            assert (await db.scalar(select(BenchmarkAgentCall))).state == "uncertain"
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        if gateway.executor.job is not None and not gateway.executor.job.done():
            await gateway.cancel()
            await asyncio.wait_for(gateway.executor.wait(), 10)


async def test_mode_rejection_is_durable_and_never_starts_an_agent(calls, tmp_path):  # noqa: F811
    gateway, started = await gateway_for(calls, tmp_path)
    request = frame(calls)
    for method in [gateway.handle, gateway.replay]:
        with pytest.raises(ProtocolError) as rejected:
            await method(request)
        assert rejected.value.code == "AGENT_MODE_CONFLICT"
    assert not started
    await gateway.cancel()


async def test_event_pages_keep_the_byte_limit_without_skipping_history(calls):  # noqa: F811
    await start(calls)
    async with AsyncSession(calls.trials.engine) as db:
        attempt = await db.get(ExecutionAttempt, calls.session_id)
        for i in range(3):
            db.add(
                ExecutionEvent(
                    attempt_id=attempt.id,
                    seq=i,
                    event="message.end",
                    event_key=str(i),
                    payload={"seq": i, "data": {"content": "x" * 400_000}},
                )
            )
        attempt.next_seq = 3
        await db.commit()
    first = await calls.event_page({"cursor": 0})
    assert first["cursor"] == 2 and len(first["events"]) == 2
    second = await calls.event_page({"cursor": first["cursor"]})
    assert second["cursor"] == 3 and len(second["events"]) == 1


async def test_unpageable_event_reports_a_failure_without_skipping_the_cursor(calls):  # noqa: F811
    await start(calls)
    async with AsyncSession(calls.trials.engine) as db:
        db.add(
            ExecutionEvent(
                attempt_id=calls.session_id,
                seq=0,
                event_key="large",
                event="message.end",
                payload={"seq": 0, "data": {"content": "x" * (1024**2 - 1024)}},
            )
        )
        attempt = await db.get(ExecutionAttempt, calls.session_id)
        attempt.next_seq = 1
        await db.commit()
    with pytest.raises(ProtocolError) as large:
        await calls.event_page({"cursor": 0})
    assert large.value.code == "AGENT_EVENT_TOO_LARGE"
    async with AsyncSession(calls.trials.engine) as db:
        assert (await db.get(ExecutionAttempt, calls.session_id)).next_seq == 1


async def test_invalid_initial_history_is_rejected_before_attempt_or_process_creation(
    calls, tmp_path
):
    gateway, started = await gateway_for(calls, tmp_path, mode="conversation")
    request = frame(calls, payload={"initial_history": [{"role": "invalid", "content": "Fixture"}]})
    with pytest.raises(ProtocolError) as invalid:
        await gateway.handle(request)
    assert invalid.value.code == "INVALID_AGENT_INPUT" and not started
    async with AsyncSession(calls.trials.engine) as db:
        assert await db.get(ExecutionAttempt, calls.session_id) is None
    with pytest.raises(ProtocolError) as duplicate:
        await gateway.replay(request)
    assert duplicate.value.code == "INVALID_AGENT_INPUT"


async def test_unsupported_control_returns_a_durable_rejection_without_poisoning_the_session(
    calls, tmp_path
):
    gateway, _ = await gateway_for(calls, tmp_path, mode="conversation")
    await gateway.handle(frame(calls))
    request = frame(
        calls, "control", request_id="unavailable", payload={"name": "unavailable", "payload": {}}
    )
    try:
        for operation in [gateway.handle, gateway.replay]:
            with pytest.raises(ProtocolError) as unsupported:
                await operation(request)
            assert unsupported.value.code == "INVALID_AGENT_COMMAND"
    finally:
        result = await asyncio.wait_for(
            gateway.handle(frame(calls, "close", request_id="close")), 15
        )
    assert result["status"] == "success"


async def test_external_tool_result_roundtrip_uses_native_revision_and_acknowledgement(
    calls, tmp_path
):
    gateway, _ = await gateway_for(calls, tmp_path, mode="external_steps")
    await gateway.handle(frame(calls))
    payload = {
        "input_id": "external-input",
        "turn_id": "external-turn",
        "expected_revision": 0,
        "messages": [{"role": "user", "content": "tools"}],
    }
    await gateway.handle(
        frame(
            calls,
            "control",
            request_id="submit",
            payload={"name": "turn.submit", "payload": payload},
        )
    )
    cursor, counter, events = 0, 0, []

    async def page_until(predicate):
        nonlocal cursor, counter

        async with asyncio.timeout(10):
            while not any(predicate(event) for event in events):
                page = await gateway.handle(
                    frame(
                        calls,
                        "events",
                        request_id="external-page-" + str(counter),
                        payload={"cursor": cursor, "wait_seconds": 1},
                    )
                )
                counter += 1
                cursor = page["cursor"]
                events.extend(page["events"])

    try:
        await page_until(lambda event: event["event"] == "tool.request")
        request = next(event["data"] for event in events if event["event"] == "tool.request")
        results = {
            "input_id": "tool-result",
            "turn_id": request["turn_id"],
            "batch_id": request["batch_id"],
            "expected_revision": request["revision"],
            "results": [{"call_id": "tool-1", "content": "Native tool output", "is_error": False}],
        }
        acknowledgement = await gateway.handle(
            frame(
                calls,
                "control",
                request_id="tool-reply",
                payload={"name": "tool.respond", "payload": results},
            )
        )
        assert acknowledgement["result"]["ok"]
        await page_until(
            lambda event: (
                event["event"] == "session.waiting_input" and event["data"]["revision"] == 1
            )
        )
        closed = await gateway.handle(frame(calls, "close", request_id="close"))
        assert closed["status"] == "success"
    finally:
        if gateway.executor.job is not None and not gateway.executor.job.done():
            await gateway.cancel()
            await asyncio.wait_for(gateway.executor.wait(), 10)
