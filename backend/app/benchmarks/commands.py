"""Persist worker intent before sending, then the wire receipt before ACK.

No database fence is held while awaiting a worker: reverse broker requests and
events must continue. Controller generations fence every subsequent commit.
"""

import uuid
from dataclasses import dataclass, field

from evalhive_benchmark import ProtocolError, encode_line, validate
from evalhive_benchmark.connector import RESULT_TYPES
from evalhive_benchmark.peer import identity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.benchmarks.trials import failure
from app.models.benchmark_command import BenchmarkCommand
from app.models.benchmark_trial import BenchmarkEventStream, BenchmarkHostReservation

COMMAND_PHASES = {
    "model.call": {"driving", "evaluating"},
    "describe": {
        "planning",
        "preparing",
        "ready",
        "driving",
        "capturing",
        "evaluating",
        "cleaning",
    },
    "list_cases": {"planning"},
    "resolve_case": {"planning"},
    "validate": {"planning"},
    "plan": {"planning"},
    "prepare": {"preparing"},
    "drive": {"driving"},
    "capture": {"capturing", "cleaning"},
    "evaluate": {"evaluating"},
    "aggregate": {"evaluating", "cleaning"},
    "cleanup": {"cleaning"},
    "cancel": {"planning", "preparing", "ready", "driving", "capturing", "evaluating", "cleaning"},
    "shutdown": {
        "planning",
        "preparing",
        "ready",
        "driving",
        "capturing",
        "evaluating",
        "cleaning",
    },
}


@dataclass(frozen=True)
class CommandTicket:
    id: uuid.UUID
    request_id: str
    replay: dict | None = field(default=None, repr=False)
    error_code: str | None = None


class CommandStore:
    def __init__(self, trials, owner, actor_id):
        self.trials, self.owner, self.actor_id = trials, owner, uuid.UUID(str(actor_id))

    async def _current(self, db):
        trial, now = await self.trials.current(db, self.owner, row_lock=True)
        if trial.phase == "finished":
            raise failure("TRIAL_FINISHED", "Finished worker journals are sealed")
        return trial, now

    async def _stream(self, db):
        stream = await db.scalar(
            select(BenchmarkEventStream).where(
                BenchmarkEventStream.trial_id == self.owner.trial_id,
                BenchmarkEventStream.control_id == self.owner.control_id,
                BenchmarkEventStream.generation == self.owner.generation,
                BenchmarkEventStream.actor_id == self.actor_id,
            )
        )
        if stream is None:
            raise failure("UNKNOWN_ACTOR", "Worker stream has not been registered")
        return stream

    async def _row(self, db, stream, command_id):
        row = await db.scalar(
            select(BenchmarkCommand)
            .where(
                BenchmarkCommand.id == command_id,
                BenchmarkCommand.stream_id == stream.id,
                BenchmarkCommand.trial_id == self.owner.trial_id,
            )
            .options(undefer(BenchmarkCommand.private_response))
        )
        if row is None:
            raise failure("UNKNOWN_COMMAND", "Command is unavailable in this worker scope")
        return row

    def _receipt(self, row):
        return self.trials.private.open("command-response:" + str(row.id), row.private_response)

    async def begin(self, frame, *, replay_only=False, authorize=None, record=None):
        validate("command", frame)
        encode_line(frame)
        expected_scope = {
            "workspace_id": str(self.owner.workspace_id),
            "trial_id": str(self.owner.trial_id),
            "generation": self.owner.generation,
        }
        params = frame["params"]
        for scope in (params.get("scope"), params.get("spec", {}).get("scope")):
            if scope is not None and scope != expected_scope:
                raise failure("STALE_GENERATION", "Command scope differs from its worker")
        async with AsyncSession(self.trials.engine, expire_on_commit=False) as db:
            trial, now = await self._current(db)
            stream = await self._stream(db)
            previous = await db.scalar(
                select(BenchmarkCommand)
                .where(
                    BenchmarkCommand.stream_id == stream.id,
                    BenchmarkCommand.request_id == frame["id"],
                )
                .options(undefer(BenchmarkCommand.private_response))
            )
            digest = identity(frame)
            if previous is not None:
                if previous.content_hash != digest:
                    raise failure("COMMAND_CONFLICT", "Command ID has different content")
                if previous.state != "completed":
                    raise failure(
                        "COMMAND_UNCERTAIN",
                        "Command needs reconciliation; it cannot be sent again",
                        "uncertain",
                    )
                return CommandTicket(
                    previous.id, frame["id"], self._receipt(previous), previous.error_code
                )
            if replay_only:
                raise failure("RECEIPT_UNAVAILABLE", "Command receipt is unavailable", "uncertain")
            if trial.phase not in COMMAND_PHASES[frame["method"]] or stream.final_seq is not None:
                raise failure("INVALID_PHASE", "Worker command is unavailable in this phase")
            if frame["method"] not in {"cancel", "capture", "cleanup", "shutdown"}:
                if trial.cancel_requested_at is not None or trial.deadline_at <= now:
                    raise failure(
                        "TRIAL_STOPPING",
                        "Trial cancellation or deadline prevents a new command",
                        "execution",
                    )
            if frame["method"] in {"prepare", "drive", "evaluate", "model.call"}:
                reservation = await db.get(BenchmarkHostReservation, self.trials.host_id)
                if reservation is None or (
                    reservation.trial_id,
                    reservation.generation,
                    reservation.controller_id,
                ) != (self.owner.trial_id, self.owner.generation, self.owner.controller_id):
                    raise failure(
                        "NO_RESERVATION",
                        "Worker execution requires its host reservation",
                        "preparation",
                    )
                if reservation.budget.get("stage", "full") != "full":
                    raise failure(
                        "GROUP_ADMISSION_REQUIRED",
                        "Worker execution requires complete group admission",
                        "preparation",
                    )
            if authorize is not None:
                await authorize(db, trial, stream, now)
            row = BenchmarkCommand(
                id=uuid.uuid4(),
                trial_id=self.owner.trial_id,
                stream_id=stream.id,
                request_id=frame["id"],
                method=frame["method"],
                phase=trial.phase,
                content_hash=digest,
                state="sent",
            )
            row.private_request = self.trials.private.seal("command-request:" + str(row.id), frame)
            db.add(row)
            if record is not None:
                await record(db, trial, row)
            await self.trials.append(
                db, trial, "command.sent", {"command_id": str(row.id), "method": row.method}
            )
            await db.commit()
            # 'sent' is committed BEFORE transmission. A crash in between may
            # lose work, but cannot justify repeating a possibly executed effect.
            return CommandTicket(row.id, row.request_id)

    async def receive(self, command_id, receipt, *, record=None):
        validate("response", receipt)
        encode_line(receipt)
        async with AsyncSession(self.trials.engine, expire_on_commit=False) as db:
            trial, now = await self._current(db)
            stream = await self._stream(db)
            row = await self._row(db, stream, command_id)
            if receipt["id"] != row.request_id:
                raise failure("COMMAND_CONFLICT", "Response ID differs from its command")
            digest = identity(receipt)
            if row.response_hash is not None:
                if row.response_hash != digest:
                    raise failure("COMMAND_CONFLICT", "Command response is immutable")
                return
            if row.state != "sent":
                raise failure(
                    "COMMAND_UNCERTAIN",
                    "Late response cannot complete an interrupted command",
                    "uncertain",
                )
            row.private_response = self.trials.private.seal(
                "command-response:" + str(row.id), receipt
            )
            row.response_hash, row.received_at, row.state = digest, now, "received"
            if record is not None:
                await record(db, trial, row, receipt)
            await self.trials.append(db, trial, "command.received", {"command_id": str(row.id)})
            await db.commit()

    async def complete(self, command_id, *, event_floor):
        if type(event_floor) is not int or event_floor < -1:
            raise failure("INVALID_BARRIER", "Event barrier is invalid", "recording")
        async with AsyncSession(self.trials.engine, expire_on_commit=False) as db:
            trial, now = await self._current(db)
            stream = await self._stream(db)
            row = await self._row(db, stream, command_id)
            if row.state == "completed":
                return CommandTicket(row.id, row.request_id, self._receipt(row), row.error_code)
            if row.state != "received":
                raise failure(
                    "COMMAND_UNCERTAIN", "Command has no complete durable response", "uncertain"
                )
            if stream.next_seq - 1 < event_floor:
                raise failure(
                    "EVENT_GAP", "Command completion requires preceding events", "recording"
                )
            receipt = self._receipt(row)
            if not receipt["ok"] and receipt["error"]["kind"] in {
                "uncertain",
                "recording",
                "transport",
            }:
                row.error_code = "CONNECTOR_OUTCOME_UNCERTAIN"
                trial.recording_status = "incomplete"
            if receipt["ok"] and row.method in RESULT_TYPES:
                try:
                    validate(RESULT_TYPES[row.method], receipt["result"])
                except ProtocolError:
                    row.error_code = "INVALID_CONNECTOR_RESULT"
                    trial.recording_status = "incomplete"
            stream.checkpoint_seq = stream.next_seq - 1
            row.event_through_seq = stream.checkpoint_seq
            row.state, row.completed_at = "completed", now
            await self.trials.append(
                db,
                trial,
                "command.completed",
                {
                    "command_id": str(row.id),
                    "event_through_seq": row.event_through_seq,
                    "valid_result": row.error_code is None,
                },
            )
            await db.commit()
            return CommandTicket(row.id, row.request_id, receipt, row.error_code)

    async def acknowledge(self, command_id):
        async with AsyncSession(self.trials.engine, expire_on_commit=False) as db:
            trial, now = await self._current(db)
            stream = await self._stream(db)
            row = await self._row(db, stream, command_id)
            if row.state != "completed":
                raise failure(
                    "ACK_BEFORE_RECEIPT",
                    "ACK requires a complete response and event barrier",
                    "recording",
                )
            if row.acknowledged_at is None:
                row.acknowledged_at = now
                await self.trials.append(
                    db, trial, "command.acknowledged", {"command_id": str(row.id)}
                )
                await db.commit()

    async def uncertain(self, command_id, *, code="WORKER_DISCONNECTED"):
        if code not in {
            "WORKER_DISCONNECTED",
            "COMMAND_TIMEOUT",
            "COMMAND_CANCELLED",
            "EVENT_DELIVERY_FAILED",
        }:
            raise ValueError("Unknown command interruption reason")
        async with AsyncSession(self.trials.engine, expire_on_commit=False) as db:
            trial, _ = await self._current(db)
            stream = await self._stream(db)
            row = await self._row(db, stream, command_id)
            if row.state not in {"completed", "uncertain"}:
                row.state, row.error_code = "uncertain", code
                trial.recording_status = "incomplete"
                await self.trials.append(
                    db, trial, "command.uncertain", {"command_id": str(row.id), "reason": code}
                )
                await db.commit()
