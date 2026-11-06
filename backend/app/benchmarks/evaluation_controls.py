"""Own a new grading invocation without acquiring the finished execution's lease."""

import uuid
from datetime import timedelta

from evalhive_benchmark.peer import identity
from sqlalchemy import func, select, update

from app.benchmarks.control_repository import SqlControlRepository
from app.benchmarks.evaluation import terminal
from app.benchmarks.regrade import retained_source
from app.benchmarks.trials import Controller, database_fence, failure
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_evaluation_control import (
    BenchmarkEvaluationControl,
    BenchmarkEvaluationJournal,
)

EVALUATION_CONTROLS = SqlControlRepository(
    BenchmarkEvaluationControl,
    BenchmarkEvaluationJournal,
    owner_key="control_id",
    journal_key="control_id",
    source_key="source_trial_id",
)


class EvaluationControls:
    """Lease/journal boundary, not a launch API or permission to resume a world.

    Resource sessions and worker streams must bind explicitly to this context
    before a runnable regrade coordinator can use it. Execution APIs reject its
    owner token rather than interpreting it as an execution controller.
    """

    def __init__(self, trials, evidence):
        self.trials, self.evidence = trials, evidence
        self.engine, self.private, self.lease_seconds = (
            trials.engine,
            trials.private,
            trials.lease_seconds,
        )

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
        # Keep the same source-input shape as initial evaluations so another
        # regrade can read a failed grading invocation without rerunning agents.
        inputs = {
            "spec": {
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
            now = await db.scalar(select(func.clock_timestamp()))
            if deadline_at.tzinfo is None or not now < deadline_at <= now + timedelta(days=1):
                raise failure("INVALID_DEADLINE", "Evaluation deadline is outside host policy")
            previous = await db.get(BenchmarkEvaluationAttempt, evaluation_id)
            if previous is not None:
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
        async with database_fence(self.engine, trial_id) as db:
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
            control.generation += 1
            control.controller_id, control.heartbeat_at = controller_id, now
            control.controller_expires_at = now + timedelta(seconds=self.lease_seconds)
            control.phase = "cleaning"  # No native conversation/state resume.
            attempt = await db.get(BenchmarkEvaluationAttempt, evaluation_id)
            if attempt.status == "running":
                await terminal(
                    db,
                    self,
                    attempt,
                    {
                        "status": "error",
                        "success": None,
                        "metrics": [],
                        "native": {},
                        "artifacts": [],
                        "completeness": {"native_result": False},
                        "error": {
                            "code": "CONTROLLER_LOST",
                            "kind": "uncertain",
                            "message": "Evaluation controller was lost",
                        },
                    },
                )
            await EVALUATION_CONTROLS.append(db, control, "controller.claimed", {"recovery": True})
            await db.commit()
            return Controller(
                workspace_id, trial_id, control.generation, controller_id, evaluation_id
            )

    async def note(self, owner, event, *, private=None):
        if event not in {"controller.observed", "controller.failed"}:
            raise ValueError("Unknown evaluator control event")
        async with database_fence(self.engine, owner.trial_id) as db:
            row, _ = await self.current(db, owner, row_lock=True)
            if row.phase == "finished":
                raise failure("TRIAL_FINISHED", "Evaluation journal is sealed")
            identifier = uuid.uuid4()
            await EVALUATION_CONTROLS.append(
                db,
                row,
                event,
                id=identifier,
                private_data=None
                if private is None
                else self.private.seal("journal:" + str(identifier), private),
            )
            await db.commit()
