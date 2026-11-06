"""Own a new grading invocation without acquiring the finished execution's lease."""

import math
import uuid
from datetime import timedelta

from evalhive_benchmark import validate
from evalhive_benchmark.peer import identity
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.benchmarks.admission import HostAdmission
from app.benchmarks.control_repository import SqlControlRepository
from app.benchmarks.evaluation import interrupt_attempt, terminal, verify_receipt
from app.benchmarks.lifecycle import ControlLifecycle
from app.benchmarks.regrade import retained_source, verify_source
from app.benchmarks.streams import StreamLedger
from app.benchmarks.trials import Controller, database_fence, failure
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_evaluation_control import (
    BenchmarkEvaluationControl,
    BenchmarkEvaluationJournal,
)
from app.models.benchmark_trial import BenchmarkHostReservation

EVALUATION_CONTROLS = SqlControlRepository(
    BenchmarkEvaluationControl,
    BenchmarkEvaluationJournal,
    owner_key="control_id",
    journal_key="control_id",
    source_key="source_trial_id",
)


class EvaluationControls(HostAdmission, ControlLifecycle, StreamLedger):
    """Lease/journal boundary, not a launch API or permission to resume a world.

    Resource sessions and worker streams bind explicitly to this context.
    Execution APIs reject its owner token rather than interpreting it as an
    execution controller. Restoration and lifecycle coordination remain separate.
    """

    journal_model = BenchmarkEvaluationJournal
    extra_note_events = frozenset({"controller.observed"})
    phases = {
        "created": {"planning", "cleaning"},
        "planning": {"preparing", "cleaning"},
        "preparing": {"ready", "cleaning"},
        "ready": {"capturing", "evaluating", "cleaning"},
        "capturing": {"evaluating", "cleaning"},
        "evaluating": {"cleaning"},
        "cleaning": {"finished"},
        "finished": set(),
    }

    def __init__(self, trials, evidence):
        self.trials, self.evidence = trials, evidence
        self.engine, self.private, self.lease_seconds = (
            trials.engine,
            trials.private,
            trials.lease_seconds,
        )

    @property
    def host_id(self):
        return self.trials.host_id

    def control_key(self, owner):
        return owner.control_id

    async def append(self, db, row, event, public_data=None, **kwargs):
        return await EVALUATION_CONTROLS.append(db, row, event, public_data, **kwargs)

    async def start_planning(self, owner):
        return await self.transition(owner, "created", "planning")

    async def outcome_status(self, db, row):
        attempt = await self.attempt(db, row)
        return "not_applicable", attempt.status

    async def attempt(self, db, row):
        attempt = await db.get(BenchmarkEvaluationAttempt, row.id)
        if attempt is None or attempt.kind != "regrade":
            raise failure("EVALUATION_NOT_FOUND", "Regrade intent is unavailable", "grading")
        return attempt

    async def phase_effects(self, db, row, target, now):
        attempt = await self.attempt(db, row)
        if target == "evaluating" and attempt.status != "running":
            raise failure("INVALID_PHASE", "Evaluation already has a terminal outcome", "grading")
        if target == "cleaning":
            if await interrupt_attempt(
                db, self, attempt, cancelled=row.cancel_requested_at is not None
            ):
                await self.append(db, row, "evaluation.finished", {"status": attempt.status})
        if target == "finished" and attempt.status == "running":
            raise failure(
                "EVALUATION_OPEN", "Evaluation must be sealed before cleanup completes", "grading"
            )

    async def fail_evaluation(self, owner, *, kind, code, cancelled=False):
        async with database_fence(self.engine, owner.trial_id) as db:
            row, _ = await self.current(db, owner, row_lock=True)
            attempt = await self.attempt(db, row)
            if await interrupt_attempt(
                db, self, attempt, kind=kind, code=code, cancelled=cancelled
            ):
                await self.append(db, row, "evaluation.finished", {"status": attempt.status})
                await db.commit()

    async def finish_evaluation(self, owner, result=None, *, attempt_id=None):
        if result is not None:
            validate("evaluation_result", result)
        async with database_fence(self.engine, owner.trial_id) as db:
            row, _ = await self.current(db, owner, row_lock=True)
            attempt = await self.attempt(db, row)
            if result is None:
                if row.phase != "cleaning":
                    raise failure(
                        "INVALID_PHASE", "Missing evaluation outcome requires cleanup", "grading"
                    )
                if await interrupt_attempt(db, self, attempt):
                    await self.append(db, row, "evaluation.finished", {"status": attempt.status})
                    await db.commit()
                return
            if attempt_id != row.id:
                raise failure(
                    "EVALUATION_NOT_FOUND", "Result does not name its evaluation intent", "grading"
                )
            if attempt.status != "running":
                if attempt.result_digest == identity(result):
                    return
                raise failure(
                    "EVALUATION_CONFLICT", "Evaluation outcome already differs", "grading"
                )
            if (attempt.origin_generation, attempt.controller_id) != (
                owner.generation,
                owner.controller_id,
            ):
                raise failure(
                    "STALE_GENERATION", "Evaluation belongs to another controller", "grading"
                )
            if row.phase != "evaluating":
                raise failure(
                    "INVALID_PHASE", "Evaluator result requires the evaluation phase", "grading"
                )
            await verify_receipt(db, self, attempt, result, control_id=row.id)
            # Re-read cancellation/deadline after receipt IO, retaining the row lock.
            await db.refresh(row)
            now = await db.scalar(select(func.clock_timestamp()))
            if row.cancel_requested_at is not None or row.deadline_at <= now:
                raise failure(
                    "TRIAL_STOPPING",
                    "Cancellation or deadline precedes evaluator completion",
                    "execution",
                )
            await terminal(db, self, attempt, result)
            await self.append(db, row, "evaluation.finished", {"status": attempt.status})
            await db.commit()

    async def create(
        self,
        workspace_id,
        trial_id,
        source_evaluation_id,
        controller_id,
        *,
        deadline_at,
        evaluation_id=None,
    ):
        evaluation_id = evaluation_id or uuid.uuid4()
        # Creation retries read their durable receipt, even if retained storage
        # becomes unavailable after the first intent was committed.
        async with database_fence(self.engine, trial_id) as db:
            previous = await db.get(BenchmarkEvaluationAttempt, evaluation_id)
            if previous is not None:
                control = await db.get(BenchmarkEvaluationControl, evaluation_id)
                if (
                    (
                        previous.workspace_id,
                        previous.trial_id,
                        previous.source_evaluation_id,
                        previous.controller_id,
                    )
                    != (workspace_id, trial_id, source_evaluation_id, controller_id)
                    or control is None
                    or control.deadline_at != deadline_at
                ):
                    raise failure("EVALUATION_CONFLICT", "Evaluation creation identity differs")
                return Controller(workspace_id, trial_id, 1, controller_id, evaluation_id)
        source = await retained_source(
            self.trials, self.evidence, workspace_id, trial_id, source_evaluation_id
        )
        if source["limits"] is None:
            raise failure(
                "SOURCE_SPEC_UNAVAILABLE", "Source resource limits were not retained", "unsupported"
            )
        # Keep the same source-input shape as initial evaluations so another
        # regrade can read a failed grading invocation without rerunning agents.
        inputs = {
            "spec": {
                "protocol_version": "1.0",
                "purpose": "evaluation",
                "checkpoint": source["checkpoint"],
                "limits": {**source["limits"]},
                "scope": {
                    "workspace_id": str(workspace_id),
                    "trial_id": str(trial_id),
                    "generation": 1,
                },
                "case": source["case"],
                "config": source["config"],
                "models": source["models"],
                "credential_refs": source["credential_refs"],
            },
            "capture": {
                "checkpoint": source["checkpoint"],
                "artifacts": source["checkpoint"]["artifacts"],
            },
            "evaluator": source["evaluator"],
            "environment_sessions": source["environment_sessions"],
            "source_evaluation_id": str(source_evaluation_id),
        }
        async with database_fence(self.engine, trial_id) as db:
            previous = await db.get(
                BenchmarkEvaluationAttempt,
                evaluation_id,
                options=[undefer(BenchmarkEvaluationAttempt.private_inputs)],
            )
            if previous is not None:
                saved = self.private.open(
                    "evaluation-inputs:" + str(evaluation_id), previous.private_inputs
                )
                inputs["spec"]["limits"]["wall_time_seconds"] = saved["spec"]["limits"][
                    "wall_time_seconds"
                ]
                if (
                    previous.workspace_id,
                    previous.trial_id,
                    previous.source_evaluation_id,
                    previous.controller_id,
                    previous.input_digest,
                ) != (
                    workspace_id,
                    trial_id,
                    source_evaluation_id,
                    controller_id,
                    identity(inputs),
                ):
                    raise failure(
                        "EVALUATION_CONFLICT", "Evaluation identity already has different inputs"
                    )
                control = await db.get(BenchmarkEvaluationControl, evaluation_id)
                if control is None or control.deadline_at != deadline_at:
                    raise failure("EVALUATION_CONFLICT", "Evaluation control identity differs")
                # Do not mint a new owner token for a recovered epoch.
                return Controller(workspace_id, trial_id, 1, controller_id, evaluation_id)
            now = await db.scalar(select(func.clock_timestamp()))
            if deadline_at.tzinfo is None or not now < deadline_at <= now + timedelta(days=1):
                raise failure("INVALID_DEADLINE", "Evaluation deadline is outside host policy")
            inputs["spec"]["limits"]["wall_time_seconds"] = math.ceil(
                (deadline_at - now).total_seconds()
            )
            validate("trial_spec", inputs["spec"])
            db.add(
                BenchmarkEvaluationAttempt(
                    id=evaluation_id,
                    trial_id=trial_id,
                    workspace_id=workspace_id,
                    source_evaluation_id=source_evaluation_id,
                    kind="regrade",
                    origin_generation=1,
                    controller_id=controller_id,
                    worker_actor_id=uuid.uuid5(controller_id, "benchmark.actor"),
                    input_digest=identity(inputs),
                    private_inputs=self.private.seal(
                        "evaluation-inputs:" + str(evaluation_id), inputs
                    ),
                    status="running",
                    verdict="unknown",
                    metrics=[],
                    started_at=now,
                )
            )
            await db.flush()
            control = BenchmarkEvaluationControl(
                id=evaluation_id,
                source_trial_id=trial_id,
                workspace_id=workspace_id,
                generation=1,
                controller_id=controller_id,
                controller_expires_at=now + timedelta(seconds=self.lease_seconds),
                heartbeat_at=now,
                deadline_at=deadline_at,
                phase="created",
                next_seq=0,
            )
            db.add(control)
            await db.flush()
            await EVALUATION_CONTROLS.append(
                db,
                control,
                "evaluation.created",
                {"source_evaluation_id": str(source_evaluation_id)},
            )
            await db.commit()
            return Controller(workspace_id, trial_id, 1, controller_id, evaluation_id)

    async def launch_inputs(self, owner):
        """Read an immutable fresh invocation, then verify retained bytes outside SQL locks."""
        async with AsyncSession(self.engine) as db:
            row, _ = await self.current(db, owner)
            attempt = await db.get(
                BenchmarkEvaluationAttempt,
                row.id,
                options=[undefer(BenchmarkEvaluationAttempt.private_inputs)],
            )
            if (
                row.phase != "created"
                or attempt.status != "running"
                or (attempt.origin_generation, attempt.controller_id)
                != (owner.generation, owner.controller_id)
            ):
                raise failure(
                    "NATIVE_RESUME_UNSUPPORTED",
                    "Evaluation launch requires its fresh invocation",
                    "uncertain",
                )
            inputs = self.private.open(
                "evaluation-inputs:" + str(attempt.id), attempt.private_inputs
            )
            if identity(inputs) != attempt.input_digest:
                raise failure(
                    "EVALUATION_CONFLICT",
                    "Stored evaluator inputs do not match their digest",
                    "recording",
                )
        validate("trial_spec", inputs["spec"])
        if inputs["spec"].get("purpose") != "evaluation":
            raise failure(
                "SOURCE_SPEC_UNAVAILABLE", "Evaluation launch spec was not retained", "unsupported"
            )
        await verify_source(
            self.evidence, owner.workspace_id, owner.trial_id, owner.control_id, inputs
        )
        return inputs

    async def current(self, db, owner, **kwargs):
        return await EVALUATION_CONTROLS.current(db, owner, **kwargs)

    async def heartbeat(self, owner):
        async with self.engine.begin() as db:
            now = func.clock_timestamp()
            row = BenchmarkEvaluationControl
            changed = await db.execute(
                update(row)
                .where(
                    row.id == owner.control_id,
                    row.source_trial_id == owner.trial_id,
                    row.workspace_id == owner.workspace_id,
                    row.generation == owner.generation,
                    row.controller_id == owner.controller_id,
                    row.controller_expires_at > now,
                    row.phase != "finished",
                )
                .values(
                    heartbeat_at=now,
                    controller_expires_at=now + timedelta(seconds=self.lease_seconds),
                )
            )
            if changed.rowcount != 1:
                raise failure("STALE_GENERATION", "Evaluation heartbeat was fenced")

    async def request_cancel(self, workspace_id, trial_id, evaluation_id):
        row = BenchmarkEvaluationControl
        async with self.engine.begin() as db:
            changed = await db.execute(
                update(row)
                .where(
                    row.id == evaluation_id,
                    row.workspace_id == workspace_id,
                    row.source_trial_id == trial_id,
                    row.phase != "finished",
                )
                .values(
                    cancel_requested_at=func.coalesce(
                        row.cancel_requested_at, func.clock_timestamp()
                    )
                )
            )
            return changed.rowcount == 1

    async def claim(self, workspace_id, trial_id, evaluation_id, controller_id):
        async with database_fence(self.engine, trial_id, host_id=self.host_id) as db:
            control = await db.get(BenchmarkEvaluationControl, evaluation_id)
            now = await db.scalar(select(func.clock_timestamp()))
            if control is None or (control.workspace_id, control.source_trial_id) != (
                workspace_id,
                trial_id,
            ):
                raise failure("TRIAL_NOT_FOUND", "Evaluation is unavailable")
            if control.phase == "finished":
                raise failure("TRIAL_FINISHED", "Finished evaluation cannot be reclaimed")
            if control.controller_expires_at > now:
                raise failure("OWNER_ACTIVE", "Evaluation already has a live controller")
            reservation = await db.get(BenchmarkHostReservation, self.host_id)
            if reservation is not None and (
                reservation.trial_id,
                reservation.generation,
                reservation.controller_id,
            ) == (trial_id, control.generation, control.controller_id):
                reservation.generation = control.generation + 1
                reservation.controller_id = controller_id
            control.generation += 1
            control.controller_id, control.heartbeat_at = controller_id, now
            control.controller_expires_at = now + timedelta(seconds=self.lease_seconds)
            control.recording_status = "incomplete"
            if control.cleanup_status != "error":
                control.cleanup_status = "pending"
            control.phase = "cleaning"  # No native conversation/state resume.
            attempt = await db.get(BenchmarkEvaluationAttempt, evaluation_id)
            if await interrupt_attempt(db, self, attempt, code="CONTROLLER_LOST"):
                await self.append(db, control, "evaluation.finished", {"status": attempt.status})
            await EVALUATION_CONTROLS.append(db, control, "controller.claimed", {"recovery": True})
            await db.commit()
            return Controller(
                workspace_id, trial_id, control.generation, controller_id, evaluation_id
            )
