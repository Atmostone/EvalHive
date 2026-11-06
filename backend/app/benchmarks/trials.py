"""Benchmark lifecycle and contiguous private/public journal on PostgreSQL.

Every ownership/phase mutation shares a session advisory fence. The physical
connection remains checked out across commits and runtime side effects. Native
read-only waits explicitly release the fence and connection, then revalidate.
Heartbeat and a monotonic cancellation request use conditional SQL separately so
long native commands cannot prevent either from reaching the controller.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import timedelta

from evalhive_benchmark import ProtocolError, validate
from evalhive_benchmark.protocol import MAX_LINE_BYTES, _json_value
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.benchmarks.control_repository import EXECUTION_CONTROLS
from app.benchmarks.streams import StreamLedger
from app.models.benchmark import BenchmarkDatasetCase, BenchmarkDatasetRevision, BenchmarkProfile
from app.models.benchmark_command import BenchmarkCommand
from app.models.benchmark_trial import (
    BenchmarkEventStream,
    BenchmarkHostReservation,
    BenchmarkJournalEntry,
    BenchmarkOperation,
    BenchmarkResourceLease,
    BenchmarkTrial,
)
from app.models.task import Task
from app.runtime.admission import check_group

PHASES = {
    "created": {"planning", "cleaning"},
    "planning": {"preparing", "cleaning"},
    "preparing": {"ready", "cleaning"},
    "ready": {"driving", "cleaning"},
    "driving": {"capturing", "cleaning"},
    "capturing": {"evaluating", "cleaning"},
    "evaluating": {"cleaning"},
    "cleaning": {"finished"},
    "finished": set(),
}


def failure(code, message, kind="configuration"):
    return ProtocolError(code, message, kind)


def _key(value):
    return int.from_bytes(
        hashlib.blake2b(value.encode(), digest_size=8).digest(), "big", signed=True
    )


@dataclass(frozen=True)
class Controller:
    workspace_id: uuid.UUID
    trial_id: uuid.UUID
    generation: int
    controller_id: uuid.UUID
    control_id: uuid.UUID | None = None


@dataclass(frozen=True)
class ControlState:
    phase: str
    cancelled: bool
    seconds_remaining: float
    execution_status: str
    evaluation_status: str
    recording_status: str
    cleanup_status: str


@asynccontextmanager
async def database_fence(engine, trial_id, *, host_id=None):
    """Never return a connection with session locks to the pool, even on cancel."""
    connection = await engine.connect()
    keys = ([] if host_id is None else [_key("benchmark-host:" + host_id)]) + [
        _key("benchmark-trial:" + str(trial_id))
    ]
    held = set()
    try:
        for key in keys:
            # No transaction lock: resource intents must commit before effects.
            await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
            held.add(key)
            await connection.commit()
        async with AsyncSession(bind=connection, expire_on_commit=False) as db:

            async def suspend():
                nonlocal connection
                await db.commit()
                for key in reversed(keys):
                    await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                    held.discard(key)
                await connection.commit()
                # Observations hold no transaction, session lock or pool slot.
                # Parallel native waits must leave connections for cancellation
                # and heartbeats, even with a small production pool.
                await db.close()
                await connection.close()
                connection = None

            async def resume():
                nonlocal connection
                if connection is None:
                    connection = await engine.connect()
                    db.bind = connection
                    db.sync_session.bind = connection.sync_connection
                for key in keys:
                    if key not in held:
                        await connection.execute(
                            text("SELECT pg_advisory_lock(:key)"), {"key": key}
                        )
                        held.add(key)
                        await connection.commit()

            db.info["benchmark_observation_fence"] = (suspend, resume)
            yield db
    finally:

        async def release():
            if connection is None:
                return
            try:
                await connection.rollback()
                for key in reversed(keys):
                    await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                await connection.commit()
            except BaseException:
                # A failed unlock must destroy the physical connection rather
                # than strand a session lock in a reusable pool connection.
                await connection.invalidate()
            finally:
                await connection.close()

        task = asyncio.create_task(release())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise


async def current_trial(db, owner: Controller, *, allow_expired=False, row_lock=False):
    return await EXECUTION_CONTROLS.current(
        db, owner, allow_expired=allow_expired, row_lock=row_lock
    )


async def append_entry(db, trial, event, public_data=None, *, private_data=None, **source):
    return await EXECUTION_CONTROLS.append(
        db, trial, event, public_data, private_data=private_data, **source
    )


class TrialStore(StreamLedger):
    journal_model = BenchmarkJournalEntry

    def __init__(self, engine, private_store, *, host_id="evalhive-rootless", lease_seconds=90):
        if type(lease_seconds) is not int or not 10 <= lease_seconds <= 600:
            raise ValueError("Controller lease must be between 10 and 600 seconds")
        self.engine, self.private = engine, private_store
        self.host_id, self.lease_seconds = host_id, lease_seconds

    async def current(self, db, owner, **kwargs):
        return await EXECUTION_CONTROLS.current(db, owner, **kwargs)

    async def append(self, db, row, event, public_data=None, **kwargs):
        return await EXECUTION_CONTROLS.append(db, row, event, public_data, **kwargs)

    def control_key(self, owner):
        return owner.trial_id

    async def create(
        self,
        workspace_id,
        *,
        snapshot,
        deadline_at,
        task_id=None,
        dataset_case_id=None,
        profile_id=None,
        retry_of_id=None,
        trial_id=None,
    ):
        """Called with a server-redacted snapshot, never a raw worker configuration."""
        workspace_id, trial_id = uuid.UUID(str(workspace_id)), trial_id or uuid.uuid4()
        _json_value(snapshot)
        if not isinstance(snapshot, dict) or len(json.dumps(snapshot).encode()) > MAX_LINE_BYTES:
            raise failure("INVALID_SNAPSHOT", "Trial snapshot exceeds its JSON object limit")
        async with database_fence(self.engine, trial_id) as db:
            now = await db.scalar(select(func.clock_timestamp()))
            if deadline_at.tzinfo is None or not now < deadline_at <= now + timedelta(days=7):
                raise failure("INVALID_DEADLINE", "Trial requires a future bounded UTC deadline")
            for model, selected in [
                (Task, task_id),
                (BenchmarkProfile, profile_id),
                (BenchmarkTrial, retry_of_id),
            ]:
                if selected is not None:
                    item = await db.get(model, selected)
                    if item is None or item.workspace_id != workspace_id:
                        raise failure(
                            "INVALID_SCOPE", "Trial input is unavailable in this workspace"
                        )
            if dataset_case_id is not None:
                case = await db.get(BenchmarkDatasetCase, dataset_case_id)
                dataset = (
                    None
                    if case is None
                    else await db.get(BenchmarkDatasetRevision, case.dataset_revision_id)
                )
                if dataset is None or dataset.workspace_id != workspace_id:
                    raise failure("INVALID_SCOPE", "Dataset case is unavailable in this workspace")
            validate(
                "scope",
                {"workspace_id": str(workspace_id), "trial_id": str(trial_id), "generation": 1},
            )
            trial = BenchmarkTrial(
                id=trial_id,
                workspace_id=workspace_id,
                task_id=task_id,
                dataset_case_id=dataset_case_id,
                profile_id=profile_id,
                retry_of_id=retry_of_id,
                snapshot=snapshot,
                deadline_at=deadline_at,
                generation=1,
                phase="created",
                next_seq=0,
            )
            db.add(trial)
            await db.flush()
            await append_entry(db, trial, "trial.created")
            await db.commit()
            return trial

    async def claim(self, workspace_id, trial_id, controller_id):
        """Expired ownership is fenced; uncertain native sessions are never resumed."""
        async with database_fence(self.engine, trial_id, host_id=self.host_id) as db:
            trial = await db.get(BenchmarkTrial, trial_id)
            now = await db.scalar(select(func.clock_timestamp()))
            if trial is None or trial.workspace_id != workspace_id:
                raise failure("TRIAL_NOT_FOUND", "Trial is unavailable in this workspace")
            if trial.phase == "finished":
                raise failure("TRIAL_FINISHED", "Finished trials cannot be reclaimed")
            if trial.controller_id is not None:
                if trial.controller_expires_at > now:
                    raise failure("OWNER_ACTIVE", "Trial already has a live controller")
                trial.generation += 1
                # A reconnect cannot recreate the native conversation/stream.
                # Retain all resource origins for scoped recovery and cleanup.
                if trial.execution_status in {"pending", "running"}:
                    trial.execution_status = "lost"
                if trial.evaluation_status == "running":
                    from app.benchmarks.evaluation import interrupt_initial

                    await interrupt_initial(
                        db, self, trial, kind="uncertain", code="CONTROLLER_LOST"
                    )
                    trial.evaluation_status = "error"
                trial.recording_status = "incomplete"
                trial.phase = "cleaning"
                if trial.cleanup_status != "error":
                    trial.cleanup_status = "pending"
            trial.controller_id = controller_id
            trial.controller_expires_at = now + timedelta(seconds=self.lease_seconds)
            trial.heartbeat_at = now
            reservation = await db.get(BenchmarkHostReservation, self.host_id)
            if reservation is not None and reservation.trial_id == trial.id:
                reservation.generation = trial.generation
                reservation.controller_id = controller_id
            await append_entry(
                db,
                trial,
                "controller.claimed",
                {"generation": trial.generation, "recovery": trial.phase == "cleaning"},
            )
            await db.commit()
            return Controller(trial.workspace_id, trial.id, trial.generation, controller_id)

    async def heartbeat(self, owner):
        async with self.engine.begin() as conn:
            now = func.clock_timestamp()
            result = await conn.execute(
                update(BenchmarkTrial)
                .where(
                    BenchmarkTrial.id == owner.trial_id,
                    BenchmarkTrial.workspace_id == owner.workspace_id,
                    BenchmarkTrial.generation == owner.generation,
                    BenchmarkTrial.controller_id == owner.controller_id,
                    BenchmarkTrial.controller_expires_at > now,
                    BenchmarkTrial.phase != "finished",
                )
                .values(
                    heartbeat_at=now,
                    controller_expires_at=now + timedelta(seconds=self.lease_seconds),
                )
            )
            if result.rowcount != 1:
                raise failure("STALE_GENERATION", "Controller heartbeat was fenced")

    async def control(self, owner):
        """Short owner-checked DB-clock snapshot; no native session fence."""
        async with AsyncSession(self.engine) as db:
            trial, now = await current_trial(db, owner)
            return ControlState(
                trial.phase,
                trial.cancel_requested_at is not None,
                (trial.deadline_at - now).total_seconds(),
                trial.execution_status,
                trial.evaluation_status,
                trial.recording_status,
                trial.cleanup_status,
            )

    async def note(
        self, owner, event, *, code=None, private=None, incomplete=False, cleanup_error=False
    ):
        if event not in {
            "controller.stopping",
            "controller.failed",
            "capture.completed",
            "capture.failed",
            "cleanup.failed",
            "deadline.result",
            "configuration.applied",
        }:
            raise ValueError("Unknown controller journal event")
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            trial, _ = await current_trial(db, owner, row_lock=True)
            if trial.phase == "finished":
                raise failure("TRIAL_FINISHED", "Finished controller journals are sealed")
            entry_id = uuid.uuid4()
            await append_entry(
                db,
                trial,
                event,
                {"code": code} if code else {},
                id=entry_id,
                private_data=None
                if private is None
                else self.private.seal("journal:" + str(entry_id), private),
            )
            if incomplete:
                trial.recording_status = "incomplete"
            if cleanup_error:
                trial.cleanup_status = "error"
            elif event == "cleanup.failed" and trial.cleanup_status != "error":
                trial.cleanup_status = "pending"
            await db.commit()

    async def begin_evaluation(self, owner, inputs):
        from app.benchmarks.evaluation import begin_initial

        return await begin_initial(self, owner, inputs)

    async def fail_evaluation(self, owner, *, kind, code, cancelled=False):
        from app.benchmarks.evaluation import interrupt_initial

        async with database_fence(self.engine, owner.trial_id) as db:
            trial, _ = await current_trial(db, owner, row_lock=True)
            if trial.phase != "evaluating" or trial.evaluation_status != "running":
                return
            await interrupt_initial(db, self, trial, kind=kind, code=code, cancelled=cancelled)
            await append_entry(
                db, trial, "evaluation.finished", {"status": trial.evaluation_status}
            )
            await db.commit()

    async def finish_evaluation(self, owner, result=None, *, attempt_id=None):
        if result is not None:
            validate("evaluation_result", result)
        async with database_fence(self.engine, owner.trial_id) as db:
            trial, _ = await current_trial(db, owner, row_lock=True)
            if result is None:
                if (
                    trial.phase not in {"capturing", "cleaning"}
                    or trial.evaluation_status != "pending"
                ):
                    return
                trial.evaluation_status = "unavailable"
            else:
                from app.benchmarks.evaluation import finish

                _, duplicate = await finish(db, self, trial, result, attempt_id=attempt_id)
                if duplicate:
                    return
                trial.evaluation_status = result["status"]
            entry_id = uuid.uuid4()
            await append_entry(
                db,
                trial,
                "evaluation.finished",
                {"status": trial.evaluation_status},
                id=entry_id,
                private_data=None
                if result is None
                else self.private.seal("journal:" + str(entry_id), result),
            )
            await db.commit()

    async def request_cancel(self, workspace_id, trial_id):
        async with self.engine.begin() as conn:
            result = await conn.execute(
                update(BenchmarkTrial)
                .where(
                    BenchmarkTrial.id == trial_id,
                    BenchmarkTrial.workspace_id == workspace_id,
                    BenchmarkTrial.phase != "finished",
                )
                .values(
                    cancel_requested_at=func.coalesce(
                        BenchmarkTrial.cancel_requested_at, func.clock_timestamp()
                    )
                )
            )
            return result.rowcount == 1

    async def reserve(
        self,
        owner,
        *,
        demands,
        native_resources,
        measure,
        runtime_policy,
        group_policy,
        stage="full",
    ):
        """Admit an isolated planner, then freeze the complete group exactly once.

        A planning reservation only authorizes one registered worker. Finalizing
        retains its exact demand; no later resize or lowering native minima is
        allowed. Measurements remain conservative while that worker is alive.
        """
        if stage not in {"planning", "full"}:
            raise failure("INVALID_ADMISSION", "Unknown host admission stage")
        if stage == "planning" and (len(demands) != 1 or native_resources):
            raise failure("INVALID_ADMISSION", "Planning admission requires one worker demand")
        resources = [asdict(item) for item in demands]
        async with database_fence(self.engine, owner.trial_id, host_id=self.host_id) as db:
            trial, now = await self.current(db, owner)
            if (
                trial.phase != "planning"
                or trial.cancel_requested_at is not None
                or trial.deadline_at <= now
            ):
                raise failure("INVALID_PHASE", "Host admission requires an active planning phase")
            reservation = await db.get(BenchmarkHostReservation, self.host_id)
            if reservation is not None:
                if (reservation.trial_id, reservation.generation, reservation.controller_id) != (
                    owner.trial_id,
                    owner.generation,
                    owner.controller_id,
                ):
                    raise failure("HOST_BUSY", "Execution host remains reserved", "preparation")
                previous_stage = reservation.budget.get("stage", "full")
                if previous_stage == stage:
                    if (
                        reservation.budget["resources"] != resources
                        or reservation.budget["native_resources"] != native_resources
                    ):
                        raise failure(
                            "ADMISSION_CONFLICT",
                            "Host reservation has different resource requirements",
                        )
                    return reservation.budget
                selected = {item["resource_id"]: item for item in resources}
                if (
                    previous_stage != "planning"
                    or stage != "full"
                    or any(
                        selected.get(item["resource_id"]) != item
                        for item in reservation.budget["resources"]
                    )
                ):
                    raise failure(
                        "ADMISSION_CONFLICT", "Final admission must preserve the planning worker"
                    )
            measured = await measure()
            # Measurement may yield while a cancellation request arrives.
            await db.refresh(trial)
            now = await db.scalar(select(func.clock_timestamp()))
            if trial.cancel_requested_at is not None or trial.deadline_at <= now:
                raise failure("TRIAL_STOPPING", "Trial stopped during host admission", "execution")
            decision = check_group(
                demands, native_resources, measured, runtime_policy, group_policy, active_trials=0
            )
            if not decision["eligible"]:
                raise failure(
                    "CAPACITY_UNAVAILABLE",
                    "Native group does not fit the execution host",
                    "preparation",
                )
            budget = {
                **decision,
                "stage": stage,
                "runtime_admission": "reserved",
                "can_run": False,
                "resources": resources,
                "native_resources": native_resources,
            }
            finalizing = reservation is not None
            if finalizing:
                reservation.budget = budget
            else:
                db.add(
                    BenchmarkHostReservation(
                        host_id=self.host_id,
                        trial_id=owner.trial_id,
                        generation=owner.generation,
                        controller_id=owner.controller_id,
                        budget=budget,
                    )
                )
            await self.append(
                db,
                trial,
                "host.reservation_finalized" if finalizing else "host.reserved",
                {"stage": stage, "group_resources": decision["group_resources"]},
            )
            await db.commit()
            return budget

    async def transition(self, owner, expected, target):
        async with database_fence(
            self.engine, owner.trial_id, host_id=self.host_id if target == "finished" else None
        ) as db:
            trial, now = await current_trial(db, owner, row_lock=True)
            if trial.phase == target:
                return {"duplicate": True, "phase": target}
            if trial.phase != expected or target not in PHASES.get(expected, set()):
                raise failure("INVALID_PHASE", "Lifecycle transition is not permitted")
            if target not in {"cleaning", "finished"}:
                if trial.cancel_requested_at is not None or trial.deadline_at <= now:
                    raise failure(
                        "TRIAL_STOPPING",
                        "Trial cancellation or deadline requires cleanup",
                        "execution",
                    )
            if target == "preparing":
                reservation = await db.get(BenchmarkHostReservation, self.host_id)
                if reservation is None or (
                    reservation.trial_id,
                    reservation.generation,
                    reservation.controller_id,
                ) != (owner.trial_id, owner.generation, owner.controller_id):
                    raise failure(
                        "NO_RESERVATION",
                        "Preparation requires an owned host reservation",
                        "preparation",
                    )
                if reservation.budget.get("stage", "full") != "full":
                    raise failure(
                        "GROUP_ADMISSION_REQUIRED",
                        "Preparation requires the complete environment group reservation",
                        "preparation",
                    )
            if target == "driving":
                trial.execution_status = "running"
            if target == "evaluating":
                if trial.execution_status in {"pending", "running"}:
                    raise failure(
                        "EXECUTION_OPEN", "Execution must be sealed before evaluation", "recording"
                    )
                trial.evaluation_status = "running"
            if target == "cleaning":
                if trial.execution_status in {"pending", "running"}:
                    trial.execution_status = "cancelled" if trial.cancel_requested_at else "failed"
                if trial.evaluation_status == "running":
                    from app.benchmarks.evaluation import interrupt_initial

                    await interrupt_initial(
                        db, self, trial, cancelled=trial.cancel_requested_at is not None
                    )
                    if trial.evaluation_status == "running":
                        trial.evaluation_status = (
                            "cancelled" if trial.cancel_requested_at else "error"
                        )
                trial.cleanup_status = "running"
            if target == "finished":
                live = await db.scalar(
                    select(BenchmarkResourceLease.id)
                    .where(
                        BenchmarkResourceLease.trial_id == trial.id,
                        BenchmarkResourceLease.state != "released",
                    )
                    .limit(1)
                )
                if live is not None:
                    raise failure(
                        "CLEANUP_PENDING", "Resource leases remain unreconciled", "cleanup"
                    )
                reservation = await db.get(BenchmarkHostReservation, self.host_id)
                if reservation is not None and reservation.trial_id == trial.id:
                    await db.delete(reservation)
                if trial.cleanup_status != "error":
                    trial.cleanup_status = "complete"
                trial.finished_at = now
                if trial.recording_status == "open":
                    unsealed = await db.scalar(
                        select(BenchmarkEventStream.id)
                        .where(
                            BenchmarkEventStream.trial_id == trial.id,
                            BenchmarkEventStream.final_seq.is_(None),
                        )
                        .limit(1)
                    )
                    uncertain = await db.scalar(
                        select(BenchmarkOperation.id)
                        .where(
                            BenchmarkOperation.trial_id == trial.id,
                            BenchmarkOperation.state.in_(["started", "uncertain"]),
                        )
                        .limit(1)
                    )
                    pending_command = await db.scalar(
                        select(BenchmarkCommand.id)
                        .where(
                            BenchmarkCommand.trial_id == trial.id,
                            BenchmarkCommand.state != "completed",
                        )
                        .limit(1)
                    )
                    trial.recording_status = (
                        "complete"
                        if unsealed is None and uncertain is None and pending_command is None
                        else "incomplete"
                    )
            trial.phase = target
            await append_entry(db, trial, "phase.changed", {"from": expected, "to": target})
            await db.commit()
            return {"duplicate": False, "phase": target}

    async def finish_execution(self, owner, status):
        if status not in {"success", "failed", "cancelled", "lost"}:
            raise failure("INVALID_OUTCOME", "Execution outcome is invalid")
        async with database_fence(self.engine, owner.trial_id) as db:
            trial, now = await current_trial(db, owner, row_lock=True)
            if trial.execution_status == status:
                return
            if status == "success" and (
                trial.cancel_requested_at is not None or trial.deadline_at <= now
            ):
                raise failure(
                    "TRIAL_STOPPING",
                    "Cancellation or deadline precedes execution completion",
                    "execution",
                )
            if trial.phase not in {"driving", "capturing"} or trial.execution_status != "running":
                raise failure("INVALID_PHASE", "Execution outcome cannot be changed")
            pending_command = await db.scalar(
                select(BenchmarkCommand.id)
                .join(BenchmarkEventStream, BenchmarkCommand.stream_id == BenchmarkEventStream.id)
                .where(
                    BenchmarkCommand.trial_id == trial.id,
                    BenchmarkEventStream.generation == owner.generation,
                    BenchmarkCommand.method.in_(["prepare", "drive"]),
                    BenchmarkCommand.state != "completed",
                )
                .limit(1)
            )
            if pending_command is not None:
                raise failure(
                    "COMMAND_OPEN",
                    "Execution completion requires durable worker responses",
                    "recording",
                )
            open_stream = await db.scalar(
                select(BenchmarkEventStream.id)
                .where(
                    BenchmarkEventStream.trial_id == trial.id,
                    BenchmarkEventStream.generation == owner.generation,
                    or_(
                        BenchmarkEventStream.checkpoint_seq.is_(None),
                        BenchmarkEventStream.checkpoint_seq != BenchmarkEventStream.next_seq - 1,
                    ),
                )
                .limit(1)
            )
            if open_stream is not None:
                raise failure(
                    "STREAM_OPEN",
                    "Execution completion requires acknowledged event barriers",
                    "recording",
                )
            trial.execution_status = status
            await append_entry(db, trial, "execution.finished", {"status": status})
            # Deliberately do not stop/release environments or finish grading.
            await db.commit()
