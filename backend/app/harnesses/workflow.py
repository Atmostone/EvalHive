"""Task review and archiving, independent of the connector's agent loop."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta

from app.auth.tokens import _utcnow_naive
from app.harnesses.attempts import TERMINAL, locked_attempt
from app.models.task import Task, TaskStatus
from app.utils.events import broadcast_committed_event, log_event

REVIEW_TIMEOUT_SECONDS = 120
REVIEW_RECOVERY_SECONDS = 300
COMPLETE = {"archived", "retry", "superseded"}


def _mark(attempt, state, **details):
    attempt.completeness = {**attempt.completeness, "workflow": state, **details}


async def _event(db, task, attempt, name, data):
    event = await log_event(
        db, name, "orchestrator", {"attempt_id": str(attempt.id), **data},
        task_id=task.id, workspace_id=task.workspace_id, commit=False,
    )

    db.info.setdefault("harness_workflow_events", []).append(event)
    return event


async def _commit(db):
    await db.commit()
    for event in db.info.pop("harness_workflow_events", []):
        with suppress(Exception):
            await broadcast_committed_event(event)


async def _rollback(db):
    db.info.pop("harness_workflow_events", None)
    await db.rollback()


async def continue_workflow(db, attempt_id):
    """Resume committed stages without repeating an uncertain paid review.

    A claim is durable before review starts. Another worker leaves it alone
    during its bounded lease; recovery sends an uncertain review to the owner
    instead of inventing approval or repeating a model call.
    """
    attempt = await locked_attempt(db, attempt_id)
    if attempt.status not in TERMINAL or attempt.completeness.get("task_projection") != "committed":
        await _commit(db)
        return
    task = await db.get(Task, attempt.task_id)
    mode = attempt.run_spec.get("metadata", {}).get("execution_mode")
    if mode not in {"direct", "orchestrated"}:
        # Low-level SDK clients may implement a different task workflow.
        from app.orchestrator.engine import check_parent_task_completion

        await check_parent_task_completion(db, task, commit=False)
        await _commit(db)
        return
    state = attempt.completeness.get("workflow", "pending")
    if state in COMPLETE:
        await _commit(db)
        return
    benchmark = bool((task.run_config or {}).get("benchmark_mode"))
    if state == "review_running":
        started = datetime.fromisoformat(attempt.completeness["review_started_at"])
        if started > _utcnow_naive() - timedelta(seconds=REVIEW_RECOVERY_SECONDS):
            await _commit(db)
            return
        if task.status == TaskStatus.REVIEW.value:
            task.status = TaskStatus.AWAITING_APPROVAL.value
        _mark(attempt, "reviewed", review="unknown_after_interruption")
        await _event(db, task, attempt, "orchestrator_decision", {"action": "review_requires_owner", "reason": "interrupted"})
        state = "reviewed"
    if state == "pending":
        cancelled = attempt.status in {"cancelled", "lost"} or (attempt.result or {}).get("error", {}).get("code") in {
            "TIMEOUT", "RUNNER_ERROR", "ARTIFACT_DELIVERY", "START_FAILED",
        }
        if mode == "orchestrated" and not benchmark and task.status == TaskStatus.FAILED.value and not cancelled:
            if task.retry_count < task.max_retries:
                task.retry_count += 1
                task.status, task.completed_at = TaskStatus.READY.value, None
                _mark(attempt, "retry")
                await _event(db, task, attempt, "task_retry", {"retry": task.retry_count, "carried_failure_type": task.failure_type})
                await _commit(db)
                return
        if mode == "orchestrated" and not benchmark and task.status == TaskStatus.DONE.value:
            task.status = TaskStatus.REVIEW.value
            _mark(attempt, "review_running", review_started_at=_utcnow_naive().isoformat())
            title, description, summary, files = task.title, task.description or "", task.result_summary or "", list(task.result_files or [])
            workspace_id, task_id = task.workspace_id, task.id
            await _commit(db)
            evaluation = None
            try:
                from app.api._resolve_model import resolve_workspace_model
                from app.orchestrator.llm import evaluate_agent_result

                model = await resolve_workspace_model(db, workspace_id, "orchestrator")
                evaluation = await asyncio.wait_for(evaluate_agent_result(
                    title, description, summary, files, model, db=db, task_id=task_id, commit=False,
                ), REVIEW_TIMEOUT_SECONDS)
                await _commit(db)
            except Exception:
                # Exceptions may contain provider credentials or native output.
                await _rollback(db)
            attempt = await locked_attempt(db, attempt_id)
            task = await db.get(Task, task_id)
            if task.status != TaskStatus.REVIEW.value:
                _mark(attempt, "superseded")
                await _commit(db)
                return
            approved = evaluation.get("approved") if isinstance(evaluation, dict) else None
            if type(approved) is not bool:
                task.status = TaskStatus.AWAITING_APPROVAL.value
                _mark(attempt, "reviewed", review="unavailable")
                await _event(db, task, attempt, "orchestrator_decision", {"action": "review_requires_owner", "reason": "unavailable"})
            elif approved:
                task.status = TaskStatus.AWAITING_APPROVAL.value
                _mark(attempt, "reviewed", review="approved")
                await _event(db, task, attempt, "orchestrator_decision", {"action": "auto_review_passed"})
            else:
                feedback = evaluation.get("feedback") or "Result did not meet requirements"
                task.orchestrator_feedback = feedback
                if task.retry_count < task.max_retries:
                    task.retry_count += 1
                    task.status, task.completed_at = TaskStatus.READY.value, None
                    _mark(attempt, "retry", review="rejected")
                    await _event(db, task, attempt, "orchestrator_feedback", {"feedback": feedback, "retry": task.retry_count})
                    await _commit(db)
                    return
                task.status = TaskStatus.FAILED.value
                _mark(attempt, "reviewed", review="rejected")
                await _event(db, task, attempt, "orchestrator_decision", {"action": "auto_review_failed", "feedback": feedback, "retries_exhausted": True})
            await _commit(db)
            attempt = await locked_attempt(db, attempt_id)
            task = await db.get(Task, attempt.task_id)
        else:
            _mark(attempt, "reviewed")
    try:
        from app.quality.data_lake import build_quality_record

        await build_quality_record(db, task, commit=False)
        _mark(attempt, "archived")
        from app.orchestrator.engine import check_parent_task_completion

        parent_event = await check_parent_task_completion(db, task, commit=False)
        if parent_event is not None:
            db.info.setdefault("harness_workflow_events", []).append(parent_event)
        resources = attempt.run_spec.get("environment", {}).get("resources", [])
        extract = mode == "orchestrated" and not benchmark and attempt.completeness.get("review") == "approved" and task.status == TaskStatus.AWAITING_APPROVAL.value and any(
            resource.get("kind") == "memory" and resource.get("config", {}).get("mode") == "structured"
            for resource in resources
        )
        await _commit(db)
    except Exception:
        await _rollback(db)
        attempt = await locked_attempt(db, attempt_id)
        task = await db.get(Task, attempt.task_id)
        _mark(attempt, "archive_pending")
        await _event(db, task, attempt, "result_archive_pending", {"reason": "storage_unavailable"})
        await _commit(db)
        return
    if extract:
        from app.memory.extractor import extract_memory

        asyncio.create_task(extract_memory(task.id))
