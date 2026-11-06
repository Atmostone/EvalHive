"""Durable evaluator history. A completed checker is distinct from a correct answer."""

import uuid
from copy import deepcopy

from evalhive_benchmark import validate
from evalhive_benchmark.peer import identity
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.benchmarks.trials import append_entry, current_trial, database_fence, failure
from app.models.benchmark_command import BenchmarkCommand
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_trial import (
    BenchmarkEnvironmentSession,
    BenchmarkEventStream,
    BenchmarkTrial,
)


async def initial(db, trial_id):
    return await db.scalar(
        select(BenchmarkEvaluationAttempt).where(
            BenchmarkEvaluationAttempt.trial_id == trial_id,
            BenchmarkEvaluationAttempt.kind == "initial",
        )
    )


async def begin_initial(store, owner, inputs):
    inputs = deepcopy(inputs)
    worker_actor = uuid.UUID(inputs["worker_actor_id"])
    async with database_fence(store.engine, owner.trial_id) as db:
        trial, now = await current_trial(db, owner, row_lock=True)
        if inputs["spec"]["scope"] != {
            "workspace_id": str(owner.workspace_id),
            "trial_id": str(owner.trial_id),
            "generation": owner.generation,
        }:
            raise failure("STALE_GENERATION", "Evaluator input scope differs", "grading")
        sessions = (
            await db.scalars(
                select(BenchmarkEnvironmentSession)
                .where(
                    BenchmarkEnvironmentSession.trial_id == trial.id,
                    BenchmarkEnvironmentSession.control_id.is_(None),
                    BenchmarkEnvironmentSession.workspace_id == trial.workspace_id,
                )
                .order_by(BenchmarkEnvironmentSession.id)
            )
        ).all()
        inputs["environment_sessions"] = [
            {
                "id": str(session.id),
                "plan_digest": session.plan_digest,
                "origin_generation": session.origin_generation,
            }
            for session in sessions
        ]
        digest = identity(inputs)
        previous = await initial(db, trial.id)
        if previous is not None:
            if (
                previous.input_digest != digest
                or previous.origin_generation != owner.generation
                or previous.controller_id != owner.controller_id
            ):
                raise failure(
                    "EVALUATION_CONFLICT", "Initial evaluation inputs already differ", "grading"
                )
            return previous.id
        if (
            trial.phase != "capturing"
            or trial.evaluation_status != "pending"
            or trial.execution_status in {"pending", "running"}
        ):
            raise failure(
                "INVALID_PHASE",
                "Initial evaluation requires sealed execution and capture",
                "grading",
            )
        if trial.cancel_requested_at is not None or trial.deadline_at <= now:
            raise failure(
                "TRIAL_STOPPING", "Cancellation or deadline precedes evaluation", "execution"
            )
        if (
            await db.scalar(
                select(BenchmarkEventStream.id).where(
                    BenchmarkEventStream.trial_id == trial.id,
                    BenchmarkEventStream.control_id.is_(None),
                    BenchmarkEventStream.actor_id == worker_actor,
                    BenchmarkEventStream.generation == owner.generation,
                )
            )
            is None
        ):
            raise failure("UNKNOWN_ACTOR", "Evaluator worker stream is not registered", "grading")
        capture = inputs["capture"]
        for artifact in capture["artifacts"]:
            validate("artifact", artifact)
        identifier = uuid.uuid5(trial.id, "initial.evaluation")
        attempt = BenchmarkEvaluationAttempt(
            id=identifier,
            trial_id=trial.id,
            workspace_id=trial.workspace_id,
            kind="initial",
            origin_generation=owner.generation,
            controller_id=owner.controller_id,
            worker_actor_id=worker_actor,
            input_digest=digest,
            private_inputs=store.private.seal("evaluation-inputs:" + str(identifier), inputs),
            status="running",
            verdict="unknown",
            metrics=[],
            started_at=now,
        )
        db.add(attempt)
        trial.phase, trial.evaluation_status = "evaluating", "running"
        await append_entry(db, trial, "phase.changed", {"from": "capturing", "to": "evaluating"})
        await append_entry(
            db, trial, "evaluation.started", {"evaluation_id": str(identifier), "kind": "initial"}
        )
        await db.commit()
        return identifier


async def finish(db, store, trial, result, *, attempt_id=None):
    """Runs inside the parent's fence/transaction and commits with its summary."""
    validate("evaluation_result", result)
    attempt = await initial(db, trial.id)
    if attempt is None or attempt_id is None or attempt.id != attempt_id:
        raise failure("EVALUATION_NOT_FOUND", "A matching evaluation intent is required", "grading")
    digest = identity(result)
    if attempt.status != "running":
        if attempt.result_digest == digest:
            return attempt, True
        raise failure(
            "EVALUATION_CONFLICT", "Evaluation already has a different outcome", "grading"
        )
    if (attempt.origin_generation, attempt.controller_id) != (
        trial.generation,
        trial.controller_id,
    ):
        raise failure("STALE_GENERATION", "Evaluation belongs to another controller", "grading")
    if trial.phase != "evaluating" or trial.evaluation_status != "running":
        raise failure("INVALID_PHASE", "Evaluation summary cannot be changed", "grading")
    await verify_receipt(db, store, attempt, result, control_id=None)
    now = await db.scalar(select(func.clock_timestamp()))
    if trial.cancel_requested_at is not None or trial.deadline_at <= now:
        raise failure(
            "TRIAL_STOPPING", "Cancellation or deadline precedes evaluator completion", "execution"
        )
    await terminal(db, store, attempt, result)
    return attempt, False


async def verify_receipt(db, store, attempt, result, *, control_id):
    """Require the exact actor/context/generation durable receipt, not RPC success."""
    command = await db.scalar(
        select(BenchmarkCommand)
        .join(
            BenchmarkEventStream,
            BenchmarkEventStream.id == BenchmarkCommand.stream_id,
        )
        .where(
            BenchmarkCommand.trial_id == attempt.trial_id,
            BenchmarkCommand.request_id == "evaluation." + str(attempt.id),
            BenchmarkCommand.method == "evaluate",
            BenchmarkEventStream.control_id == control_id,
            BenchmarkEventStream.actor_id == attempt.worker_actor_id,
            BenchmarkEventStream.generation == attempt.origin_generation,
        )
        .options(undefer(BenchmarkCommand.private_response))
    )
    if command is None or command.state != "completed" or command.error_code is not None:
        raise failure(
            "COMMAND_OPEN", "Evaluation requires its exact durable worker receipt", "recording"
        )
    response = store.private.open("command-response:" + str(command.id), command.private_response)
    if not response.get("ok") or identity(response["result"]) != identity(result):
        raise failure(
            "EVALUATION_CONFLICT", "Evaluator result differs from its durable receipt", "grading"
        )


async def terminal(db, store, attempt, result):
    # Fetch time before making the ORM row dirty: terminal fields form one
    # database-guarded update and must not be autoflushed in a partial state.
    finished_at = await db.scalar(select(func.clock_timestamp()))
    attempt.status = result["status"]
    attempt.success = result["success"] if result["status"] == "completed" else None
    attempt.verdict = (
        "unknown" if attempt.success is None else "correct" if attempt.success else "incorrect"
    )
    attempt.metrics, attempt.primary_metric = (
        deepcopy(result["metrics"]),
        result.get("primary_metric"),
    )
    attempt.error_kind = result.get("error", {}).get("kind")
    attempt.private_result = store.private.seal("evaluation-result:" + str(attempt.id), result)
    attempt.result_digest, attempt.finished_at = identity(result), finished_at


async def interrupt_initial(
    db, store, trial, *, kind="uncertain", code="EVALUATION_INTERRUPTED", cancelled=False
):
    attempt = await initial(db, trial.id)
    if attempt is None or attempt.status != "running":
        return
    await interrupt_attempt(db, store, attempt, kind=kind, code=code, cancelled=cancelled)
    trial.evaluation_status = attempt.status


async def interrupt_attempt(
    db, store, attempt, *, kind="uncertain", code="EVALUATION_INTERRUPTED", cancelled=False
):
    if attempt.status != "running":
        return False
    result = {
        "status": "cancelled" if cancelled else "error",
        "success": None,
        "metrics": [],
        "native": {},
        "artifacts": [],
        "completeness": {"native_result": False},
        "error": {
            "code": code,
            "kind": kind,
            "message": "Native evaluation did not produce a confirmed result",
        },
    }
    validate("evaluation_result", result)
    await terminal(db, store, attempt, result)
    return True


def public_attempt(attempt):
    outcome = (
        attempt.verdict
        if attempt.status == "completed"
        else "running"
        if attempt.status == "running"
        else "cancelled"
        if attempt.status == "cancelled"
        else "unavailable"
        if attempt.status == "unavailable"
        else "grading_error"
        if attempt.error_kind == "grading"
        else "recording_error"
        if attempt.error_kind == "recording"
        else "infra_error"
    )
    return {
        "id": str(attempt.id),
        "trial_id": str(attempt.trial_id),
        "kind": attempt.kind,
        "status": attempt.status,
        "verdict": attempt.verdict,
        "outcome": outcome,
        "success": attempt.success,
        "metrics": deepcopy(attempt.metrics),
        "primary_metric": attempt.primary_metric,
        "error_kind": attempt.error_kind,
        "started_at": attempt.started_at.isoformat(),
        "finished_at": attempt.finished_at.isoformat() if attempt.finished_at else None,
    }


async def history(store, workspace_id, trial_id):
    async with AsyncSession(store.engine) as db:
        if (
            await db.scalar(
                select(BenchmarkTrial.id).where(
                    BenchmarkTrial.id == trial_id, BenchmarkTrial.workspace_id == workspace_id
                )
            )
            is None
        ):
            raise failure("TRIAL_NOT_FOUND", "Benchmark trial is unavailable")
        attempts = (
            await db.scalars(
                select(BenchmarkEvaluationAttempt)
                .where(
                    BenchmarkEvaluationAttempt.trial_id == trial_id,
                    BenchmarkEvaluationAttempt.workspace_id == workspace_id,
                )
                .order_by(BenchmarkEvaluationAttempt.started_at, BenchmarkEvaluationAttempt.id)
            )
        ).all()
        return [public_attempt(attempt) for attempt in attempts]
