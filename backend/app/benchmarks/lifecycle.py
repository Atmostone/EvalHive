"""Shared fenced lifecycle and cleanup accounting for execution and grading owners."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.benchmark_agent_call import BenchmarkAgentCall
from app.models.benchmark_command import BenchmarkCommand
from app.models.benchmark_evaluation_input import BenchmarkEvaluationInput
from app.models.benchmark_trial import (
    BenchmarkEnvironmentSession,
    BenchmarkEventStream,
    BenchmarkHostReservation,
    BenchmarkOperation,
    BenchmarkResourceLease,
)
from app.models.execution_attempt import ExecutionAttempt

NOTE_EVENTS = {
    "controller.stopping",
    "controller.failed",
    "capture.completed",
    "capture.failed",
    "cleanup.failed",
    "deadline.result",
    "configuration.applied",
}


class ControlLifecycle:
    extra_note_events = frozenset()

    async def request_stop(self, owner):
        """A controller may cancel only its own epoch; user cancellation is separate."""
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            row, now = await self.current(db, owner, row_lock=True)
            if row.phase == "finished":
                return False
            row.cancel_requested_at = row.cancel_requested_at or now
            await db.commit()
            return True

    async def control(self, owner):
        """Short owner-checked DB-clock snapshot; no native session fence."""
        from app.benchmarks.trials import ControlState

        async with AsyncSession(self.engine) as db:
            row, now = await self.current(db, owner)
            execution, evaluation = await self.outcome_status(db, row)
            return ControlState(
                row.phase,
                row.cancel_requested_at is not None,
                (row.deadline_at - now).total_seconds(),
                execution,
                evaluation,
                row.recording_status,
                row.cleanup_status,
            )

    async def note(
        self, owner, event, *, code=None, private=None, incomplete=False, cleanup_error=False
    ):
        from app.benchmarks.trials import failure

        if event not in NOTE_EVENTS | self.extra_note_events:
            raise ValueError("Unknown controller journal event")
        async with AsyncSession(self.engine, expire_on_commit=False) as db:
            row, _ = await self.current(db, owner, row_lock=True)
            if row.phase == "finished":
                raise failure("TRIAL_FINISHED", "Finished controller journals are sealed")
            entry_id = uuid.uuid4()
            await self.append(
                db,
                row,
                event,
                {"code": code} if code else {},
                id=entry_id,
                private_data=None
                if private is None
                else self.private.seal("journal:" + str(entry_id), private),
            )
            if incomplete:
                row.recording_status = "incomplete"
            if cleanup_error:
                row.cleanup_status = "error"
            elif event == "cleanup.failed" and row.cleanup_status != "error":
                row.cleanup_status = "pending"
            await db.commit()

    async def transition(self, owner, expected, target):
        from app.benchmarks.trials import database_fence, failure

        async with database_fence(
            self.engine, owner.trial_id, host_id=self.host_id if target == "finished" else None
        ) as db:
            row, now = await self.current(db, owner, row_lock=True)
            if row.phase == target:
                return {"duplicate": True, "phase": target}
            if row.phase != expected or target not in self.phases.get(expected, set()):
                raise failure("INVALID_PHASE", "Lifecycle transition is not permitted")
            if target not in {"cleaning", "finished"} and (
                row.cancel_requested_at is not None or row.deadline_at <= now
            ):
                raise failure(
                    "TRIAL_STOPPING", "Cancellation or deadline requires cleanup", "execution"
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
            await self.phase_effects(db, row, target, now)
            if target == "cleaning":
                if row.cleanup_status != "error":
                    row.cleanup_status = "running"
            if target == "finished":
                await self._finish_control(db, owner, row, now)
            row.phase = target
            await self.append(db, row, "phase.changed", {"from": expected, "to": target})
            await db.commit()
            return {"duplicate": False, "phase": target}

    async def _finish_control(self, db, owner, row, now):
        from app.benchmarks.trials import failure

        live = await db.scalar(
            select(BenchmarkResourceLease.id)
            .join(BenchmarkEnvironmentSession)
            .where(
                BenchmarkResourceLease.trial_id == owner.trial_id,
                BenchmarkEnvironmentSession.control_id == owner.control_id,
                BenchmarkResourceLease.state != "released",
            )
            .limit(1)
        )
        if live is not None:
            raise failure("CLEANUP_PENDING", "Resource leases remain unreconciled", "cleanup")
        if owner.control_id is None:
            active_agent = await db.scalar(
                select(BenchmarkAgentCall.id)
                .join(ExecutionAttempt, ExecutionAttempt.id == BenchmarkAgentCall.session_id)
                .where(
                    BenchmarkAgentCall.trial_id == owner.trial_id,
                    ExecutionAttempt.status.in_(["created", "running", "stopping"]),
                )
                .limit(1)
            )
            if active_agent is not None:
                raise failure("CLEANUP_PENDING", "Agent attempts remain unreconciled", "cleanup")
        if owner.control_id is not None:
            retained = await db.scalar(
                select(BenchmarkEvaluationInput.id)
                .where(
                    BenchmarkEvaluationInput.control_id == owner.control_id,
                    BenchmarkEvaluationInput.state != "released",
                )
                .limit(1)
            )
            if retained is not None:
                raise failure(
                    "CLEANUP_PENDING", "Retained input files remain unreconciled", "cleanup"
                )
        reservation = await db.get(BenchmarkHostReservation, self.host_id)
        if reservation is not None and (
            reservation.trial_id,
            reservation.generation,
            reservation.controller_id,
        ) == (owner.trial_id, owner.generation, owner.controller_id):
            await db.delete(reservation)
        if row.cleanup_status != "error":
            row.cleanup_status = "complete"
        if row.recording_status != "open":
            row.finished_at = now
            return
        unsealed = await db.scalar(
            select(BenchmarkEventStream.id)
            .where(
                BenchmarkEventStream.trial_id == owner.trial_id,
                BenchmarkEventStream.control_id == owner.control_id,
                BenchmarkEventStream.final_seq.is_(None),
            )
            .limit(1)
        )
        uncertain = await db.scalar(
            select(BenchmarkOperation.id)
            .where(
                BenchmarkOperation.trial_id == owner.trial_id,
                BenchmarkOperation.control_id == owner.control_id,
                BenchmarkOperation.state.in_(["started", "uncertain"]),
            )
            .limit(1)
        )
        pending = await db.scalar(
            select(BenchmarkCommand.id)
            .join(BenchmarkEventStream)
            .where(
                BenchmarkCommand.trial_id == owner.trial_id,
                BenchmarkEventStream.control_id == owner.control_id,
                BenchmarkCommand.state != "completed",
            )
            .limit(1)
        )
        agent_pending = None
        if owner.control_id is None:
            agent_pending = await db.scalar(
                select(BenchmarkAgentCall.id)
                .where(
                    BenchmarkAgentCall.trial_id == owner.trial_id,
                    BenchmarkAgentCall.state != "completed",
                )
                .limit(1)
            )
        row.recording_status = (
            "complete"
            if unsealed is None and uncertain is None and pending is None and agent_pending is None
            else "incomplete"
        )
        row.finished_at = now
