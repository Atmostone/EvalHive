"""Commit reverse agent intent before effects and authoritative receipts before ACK.

The existing Harness execution attempt is also the agent session. Database locks
cover short transitions only; model execution and native waits belong outside.
"""

import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from evalhive_benchmark import encode_line, validate
from evalhive_benchmark.peer import identity
from evalhive_benchmark.protocol import MAX_LINE_BYTES
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.benchmarks.harness_outcome import HarnessExecutionBinding, HarnessExecutionOutcome
from app.benchmarks.trials import database_fence, failure
from app.harnesses.attempts import mark_attempt_lost
from app.models.benchmark_agent_call import BenchmarkAgentCall
from app.models.benchmark_trial import (
    BenchmarkEnvironmentSession,
    BenchmarkEventStream,
    BenchmarkHostReservation,
    BenchmarkResourceLease,
)
from app.models.execution_attempt import ExecutionAttempt, ExecutionCommand, ExecutionEvent
from app.models.task import Task

RESULTS = {
    "run": "agent_completion",
    "open": "agent_session",
    "control": "agent_control_result",
    "events": "agent_events",
    "close": "agent_completion",
}


@dataclass(frozen=True)
class AgentCallTicket:
    id: uuid.UUID
    session_id: uuid.UUID
    replay: dict | None = field(default=None, repr=False)


class AgentCallStore:
    def __init__(self, trials, owner, actor_id):
        if owner.control_id is not None:
            raise ValueError("Evaluation controllers cannot provide an agent API")
        self.trials, self.owner, self.actor_id = trials, owner, uuid.UUID(str(actor_id))
        self.session_id = uuid.uuid5(owner.controller_id, "benchmark.harness:" + str(self.actor_id))

    async def _current(self, db, *, starting=False):
        trial, now = await self.trials.current(db, self.owner, row_lock=True)
        if trial.phase == "finished":
            raise failure("TRIAL_FINISHED", "Finished agent journals are sealed")
        if starting:
            if trial.phase != "driving":
                raise failure("INVALID_PHASE", "Agent calls require the driving phase")
            if trial.cancel_requested_at is not None or trial.deadline_at <= now:
                raise failure("TRIAL_STOPPING", "Trial cannot accept new agent calls", "execution")
            reservation = await db.get(BenchmarkHostReservation, self.trials.host_id)
            if reservation is None or (
                reservation.trial_id,
                reservation.generation,
                reservation.controller_id,
            ) != (self.owner.trial_id, self.owner.generation, self.owner.controller_id):
                raise failure("NO_RESERVATION", "Agent execution requires its host reservation")
            if reservation.budget.get("stage", "full") != "full":
                raise failure("GROUP_ADMISSION_REQUIRED", "Agent execution requires full admission")
        if trial.task_id is None:
            raise failure("AGENT_TASK_REQUIRED", "Agent execution requires an associated task")
        stream = await db.scalar(
            select(BenchmarkEventStream).where(
                BenchmarkEventStream.trial_id == self.owner.trial_id,
                BenchmarkEventStream.control_id.is_(None),
                BenchmarkEventStream.generation == self.owner.generation,
                BenchmarkEventStream.actor_id == self.actor_id,
            )
        )
        if stream is None:
            raise failure("UNKNOWN_ACTOR", "Agent caller has no registered worker stream")
        if starting and stream.final_seq is not None:
            raise failure("STREAM_CLOSED", "A sealed worker cannot issue agent calls")
        return trial, stream, now

    def _binding(self, trial):
        return HarnessExecutionBinding(
            self.owner.workspace_id,
            trial.task_id,
            self.owner.trial_id,
            self.owner.generation,
            self.owner.controller_id,
        )

    async def _attempt(self, db, trial):
        attempt = await db.get(ExecutionAttempt, self.session_id)
        if (
            attempt is None
            or (attempt.workspace_id, attempt.task_id) != (self.owner.workspace_id, trial.task_id)
            or attempt.run_spec.get("metadata", {}).get("benchmark_binding")
            != self._binding(trial).metadata()
        ):
            raise failure("FOREIGN_HARNESS_ATTEMPT", "Agent session is unavailable in this context")
        return attempt

    async def _call(self, db, stream, call_id):
        row = await db.scalar(
            select(BenchmarkAgentCall)
            .where(
                BenchmarkAgentCall.id == call_id,
                BenchmarkAgentCall.stream_id == stream.id,
                BenchmarkAgentCall.trial_id == self.owner.trial_id,
            )
            .options(
                undefer(BenchmarkAgentCall.private_request),
                undefer(BenchmarkAgentCall.private_response),
            )
        )
        if row is None:
            raise failure("UNKNOWN_AGENT_CALL", "Agent call is unavailable in this worker scope")
        return row

    def _response(self, row):
        return self.trials.private.open("agent-response:" + str(row.id), row.private_response)

    async def begin(self, frame, *, replay_only=False):
        validate("command", frame)
        encode_line(frame)
        scope = {
            "workspace_id": str(self.owner.workspace_id),
            "trial_id": str(self.owner.trial_id),
            "generation": self.owner.generation,
        }
        if frame["method"] != "agent.call" or frame["params"]["scope"] != scope:
            raise failure("FOREIGN_AGENT_CALL", "Agent command differs from its worker scope")
        params, digest = frame["params"], identity(frame)
        async with AsyncSession(self.trials.engine, expire_on_commit=False) as db:
            trial, stream, _ = await self._current(db)
            row = await db.scalar(
                select(BenchmarkAgentCall)
                .where(
                    BenchmarkAgentCall.stream_id == stream.id,
                    BenchmarkAgentCall.request_id == frame["id"],
                )
                .options(undefer(BenchmarkAgentCall.private_response))
            )
            if row is not None:
                if row.content_hash != digest:
                    raise failure("AGENT_CALL_CONFLICT", "Agent call ID has different content")
                if row.state != "completed":
                    raise failure(
                        "AGENT_CALL_UNCERTAIN", "Agent call cannot be executed again", "uncertain"
                    )
                return AgentCallTicket(row.id, self.session_id, self._response(row))
            if replay_only:
                raise failure(
                    "RECEIPT_UNAVAILABLE", "Agent call has no retained receipt", "uncertain"
                )
            await self._current(db, starting=True)
            starter = await db.scalar(
                select(BenchmarkAgentCall).where(
                    BenchmarkAgentCall.stream_id == stream.id,
                    BenchmarkAgentCall.operation.in_(["run", "open"]),
                )
            )
            operation = params["operation"]
            if operation in {"run", "open"}:
                if starter is not None:
                    raise failure(
                        "AGENT_SESSION_EXISTS", "This worker already requested an agent session"
                    )
                attempt_id = None
            else:
                if starter is None or params["session_id"] != str(self.session_id):
                    raise failure("FOREIGN_AGENT_SESSION", "Agent session differs from this worker")
                if starter.attempt_id is None or starter.state == "uncertain":
                    raise failure(
                        "AGENT_START_UNCERTAIN",
                        "Agent startup requires reconciliation",
                        "uncertain",
                    )
                await self._attempt(db, trial)
                attempt_id = self.session_id
            row = BenchmarkAgentCall(
                id=uuid.uuid4(),
                session_id=self.session_id,
                trial_id=self.owner.trial_id,
                stream_id=stream.id,
                attempt_id=attempt_id,
                request_id=frame["id"],
                operation=operation,
                content_hash=digest,
                state="started",
            )
            row.private_request = self.trials.private.seal("agent-request:" + str(row.id), frame)
            db.add(row)
            await self.trials.append(
                db, trial, "agent.call.started", {"call_id": str(row.id), "operation": operation}
            )
            await db.commit()
            return AgentCallTicket(row.id, self.session_id)

    async def bind_attempt(self, call_id):
        """Bind a created Harness attempt before starting any native agent effect."""
        async with AsyncSession(self.trials.engine, expire_on_commit=False) as db:
            trial, stream, _ = await self._current(db, starting=True)
            row = await self._call(db, stream, call_id)
            if row.operation not in {"run", "open"} or row.state != "started":
                raise failure(
                    "AGENT_CALL_CONFLICT", "Only an uncompleted startup can bind an attempt"
                )
            attempt = await self._attempt(db, trial)
            task = await db.get(Task, trial.task_id)
            if attempt.status != "created" or task.current_attempt_id != attempt.id:
                raise failure(
                    "AGENT_ALREADY_STARTED",
                    "A fresh current attempt must be bound before execution",
                )
            if row.attempt_id is None:
                row.attempt_id = attempt.id
                await self.trials.append(
                    db,
                    trial,
                    "agent.attempt.bound",
                    {"call_id": str(row.id), "attempt_id": str(attempt.id)},
                )
                await db.commit()

    async def _verify_result(self, db, trial, row, result):
        validate(RESULTS[row.operation], result)
        if row.attempt_id != self.session_id or result["session_id"] != str(self.session_id):
            raise failure(
                "AGENT_SESSION_CONFLICT", "Agent reply differs from its bound attempt", "recording"
            )
        attempt = await self._attempt(db, trial)
        if row.operation in {"run", "close"}:
            outcome = HarnessExecutionOutcome.from_attempt(attempt, self._binding(trial))
            outcome.confirm_reference(result["outcome"])
            if result["status"] != outcome.status:
                raise failure(
                    "HARNESS_RESULT_CONFLICT",
                    "Agent reply differs from its committed outcome",
                    "recording",
                )
        elif row.operation == "open":
            if result["attempt_id"] != str(attempt.id):
                raise failure(
                    "AGENT_SESSION_CONFLICT", "Session reply names another attempt", "recording"
                )
        elif row.operation == "control":
            try:
                command_id = uuid.UUID(result["command_id"])
            except ValueError:
                raise failure(
                    "AGENT_COMMAND_CONFLICT", "Agent command ID is invalid", "recording"
                ) from None
            command = await db.get(ExecutionCommand, command_id)
            request = self.trials.private.open("agent-request:" + str(row.id), row.private_request)
            if (
                command is None
                or command.attempt_id != attempt.id
                or command.result != result["result"]
                or (command.name, command.payload)
                != (request["params"]["payload"]["name"], request["params"]["payload"]["payload"])
            ):
                raise failure(
                    "AGENT_COMMAND_CONFLICT",
                    "Control reply has no matching native acknowledgement",
                    "recording",
                )
        else:
            request = self.trials.private.open("agent-request:" + str(row.id), row.private_request)
            payload = request["params"]["payload"]
            start, end = payload["cursor"], result["cursor"]
            if end < start or end > attempt.next_seq or end - start > payload.get("limit", 256):
                raise failure(
                    "AGENT_EVENT_CONFLICT", "Event cursor exceeds committed history", "recording"
                )
            events = list(
                await db.scalars(
                    select(ExecutionEvent.payload)
                    .where(
                        ExecutionEvent.attempt_id == attempt.id,
                        ExecutionEvent.seq >= start,
                        ExecutionEvent.seq < end,
                    )
                    .order_by(ExecutionEvent.seq)
                )
            )
            if (
                len(events) != end - start
                or events != result["events"]
                or result["closed"]
                and attempt.status not in {"success", "failed", "cancelled", "lost"}
            ):
                raise failure(
                    "AGENT_EVENT_CONFLICT", "Agent page differs from committed events", "recording"
                )

    async def complete(self, call_id, receipt):
        validate("response", receipt)
        encode_line(receipt)
        async with AsyncSession(self.trials.engine, expire_on_commit=False) as db:
            trial, stream, now = await self._current(db)
            row = await self._call(db, stream, call_id)
            if receipt["id"] != row.request_id:
                raise failure("AGENT_CALL_CONFLICT", "Agent response ID differs from its intent")
            digest = identity(receipt)
            if row.state == "completed":
                if row.response_hash != digest:
                    raise failure("AGENT_CALL_CONFLICT", "Agent receipt is immutable")
                return
            if row.state != "started":
                raise failure(
                    "AGENT_CALL_UNCERTAIN",
                    "Late replies cannot complete an interrupted call",
                    "uncertain",
                )
            if receipt["ok"]:
                await self._verify_result(db, trial, row, receipt["result"])
            elif receipt["error"]["kind"] in {"uncertain", "recording", "transport"}:
                trial.recording_status = "incomplete"
            row.private_response = self.trials.private.seal(
                "agent-response:" + str(row.id), receipt
            )
            row.state, row.response_hash, row.completed_at = "completed", digest, now
            await self.trials.append(
                db, trial, "agent.call.completed", {"call_id": str(row.id), "ok": receipt["ok"]}
            )
            await db.commit()

    async def uncertain(self, call_id):
        async with AsyncSession(self.trials.engine, expire_on_commit=False) as db:
            trial, stream, _ = await self._current(db)
            row = await self._call(db, stream, call_id)
            if row.state == "started":
                row.state, trial.recording_status = "uncertain", "incomplete"
                await self.trials.append(
                    db, trial, "agent.call.uncertain", {"call_id": str(row.id)}
                )
                await db.commit()

    async def outcome(self, reference):
        """Resolve drive's opaque reference using the committed Harness journal."""
        async with AsyncSession(self.trials.engine, expire_on_commit=False) as db:
            trial, stream, _ = await self._current(db)
            starter = await db.scalar(
                select(BenchmarkAgentCall).where(
                    BenchmarkAgentCall.stream_id == stream.id,
                    BenchmarkAgentCall.operation.in_(["run", "open"]),
                    BenchmarkAgentCall.attempt_id == self.session_id,
                )
            )
            if starter is None:
                raise failure(
                    "AGENT_SESSION_MISSING", "Drive has no bound agent session", "recording"
                )
            result = HarnessExecutionOutcome.from_attempt(
                await self._attempt(db, trial), self._binding(trial)
            )
            result.confirm_reference(reference)
            return result

    async def completion(self):
        async with self.runner_transaction() as (_, trial, attempt, _):
            outcome = HarnessExecutionOutcome.from_attempt(attempt, self._binding(trial))
            return {
                "session_id": str(attempt.id),
                "status": outcome.status,
                "outcome": outcome.reference(),
            }

    async def event_page(self, payload):
        """Return a byte-bounded prefix; the cursor never skips an oversized event."""
        async with self.runner_transaction() as (db, _, attempt, _):
            cursor = payload["cursor"]
            if cursor > attempt.next_seq:
                raise failure("AGENT_EVENT_CONFLICT", "Event cursor exceeds committed history")
            rows = await db.scalars(
                select(ExecutionEvent.payload)
                .where(
                    ExecutionEvent.attempt_id == attempt.id,
                    ExecutionEvent.seq >= cursor,
                )
                .order_by(ExecutionEvent.seq)
                .limit(payload.get("limit", 256))
            )
            events, size = [], 4096
            for event in rows:
                size += (
                    len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()) + 1
                )
                if size > MAX_LINE_BYTES:
                    if not events:
                        raise failure(
                            "AGENT_EVENT_TOO_LARGE",
                            "Native event cannot fit an agent reply",
                            "recording",
                        )
                    break
                events.append(event)
            return {
                "session_id": str(attempt.id),
                "events": events,
                "cursor": cursor + len(events),
                "closed": attempt.status in {"success", "failed", "cancelled", "lost"},
            }

    @asynccontextmanager
    async def runner_transaction(self):
        """Authorize one short Harness write without projecting a benchmark task."""
        async with AsyncSession(self.trials.engine, expire_on_commit=False) as db:
            trial, stream, now = await self._current(db)
            starter = await db.scalar(
                select(BenchmarkAgentCall.id).where(
                    BenchmarkAgentCall.stream_id == stream.id,
                    BenchmarkAgentCall.operation.in_(["run", "open"]),
                    BenchmarkAgentCall.attempt_id == self.session_id,
                )
            )
            if starter is None:
                raise failure("AGENT_NOT_BOUND", "Agent execution has no bound startup intent")
            attempt = await self._attempt(db, trial)
            yield db, trial, attempt, now
            await db.commit()

    async def reconcile(self):
        """Retire intended attempts only after all owned native resources are gone.

        A recovery controller may reconcile earlier generations, never restart
        their agent or reuse their commands. The provider's released leases are
        the stop proof, including attempts whose startup never reached binding.
        """
        async with database_fence(self.trials.engine, self.owner.trial_id) as db:
            trial, _ = await self.trials.current(db, self.owner, row_lock=True)
            if trial.phase != "cleaning":
                raise failure("INVALID_PHASE", "Agent reconciliation requires cleanup")
            live = await db.scalar(
                select(BenchmarkResourceLease.id)
                .join(BenchmarkEnvironmentSession)
                .where(
                    BenchmarkResourceLease.trial_id == self.owner.trial_id,
                    BenchmarkEnvironmentSession.control_id.is_(None),
                    BenchmarkResourceLease.state != "released",
                )
                .limit(1)
            )
            if live is not None:
                raise failure(
                    "CLEANUP_PENDING",
                    "Native leases must be released before agent reconciliation",
                    "cleanup",
                )
            starters = await db.execute(
                select(BenchmarkAgentCall, BenchmarkEventStream)
                .join(
                    BenchmarkEventStream,
                    BenchmarkEventStream.id == BenchmarkAgentCall.stream_id,
                )
                .where(
                    BenchmarkAgentCall.trial_id == self.owner.trial_id,
                    BenchmarkAgentCall.operation.in_(["run", "open"]),
                )
            )
            retired = 0
            for call, stream in starters:
                attempt = await db.get(ExecutionAttempt, call.session_id)
                if attempt is None or attempt.status in {"success", "failed", "cancelled", "lost"}:
                    continue
                binding = attempt.run_spec.get("metadata", {}).get("benchmark_binding", {})
                try:
                    controller_id = uuid.UUID(binding["controller_id"])
                    expected_id = uuid.uuid5(
                        controller_id, "benchmark.harness:" + str(stream.actor_id)
                    )
                except (KeyError, ValueError, TypeError):
                    raise failure(
                        "FOREIGN_HARNESS_ATTEMPT", "Agent recovery binding is invalid"
                    ) from None
                expected = HarnessExecutionBinding(
                    self.owner.workspace_id,
                    attempt.task_id,
                    self.owner.trial_id,
                    stream.generation,
                    controller_id,
                )
                if (
                    attempt.workspace_id != self.owner.workspace_id
                    or call.session_id != expected_id
                    or binding != expected.metadata()
                    or (trial.task_id is not None and attempt.task_id != trial.task_id)
                ):
                    raise failure(
                        "FOREIGN_HARNESS_ATTEMPT",
                        "Agent recovery binding differs from the intended session",
                    )
                await mark_attempt_lost(
                    db,
                    attempt.id,
                    "Benchmark resources were reconciled without confirmed agent completion",
                )
                retired += 1
            pending = list(
                await db.scalars(
                    select(BenchmarkAgentCall).where(
                        BenchmarkAgentCall.trial_id == self.owner.trial_id,
                        BenchmarkAgentCall.state == "started",
                    )
                )
            )
            for call in pending:
                call.state = "uncertain"
            if retired or pending:
                trial.recording_status = "incomplete"
                await self.trials.append(
                    db,
                    trial,
                    "agent.reconciled",
                    {"attempts_lost": retired, "calls_uncertain": len(pending)},
                )
            await db.commit()
            return {"attempts_lost": retired, "calls_uncertain": len(pending)}
