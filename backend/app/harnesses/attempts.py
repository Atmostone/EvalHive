"""Atomic attempt journal. Runtime and legacy task projection live elsewhere."""

from __future__ import annotations

import hashlib
import uuid
from copy import deepcopy
from datetime import datetime

from evalhive_harness import ProtocolError, encode_line, validate, validate_run
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import _utcnow_naive
from app.harnesses.profiles import _reject_inline_secrets
from app.models.execution_attempt import ExecutionAttempt, ExecutionCommand, ExecutionEvent
from app.models.harness_profile import HarnessProfile
from app.models.task import Task

TERMINAL = frozenset({"success", "failed", "cancelled", "lost"})
FENCED_ERRORS = frozenset({"STALE_ATTEMPT", "NOT_FOUND", "INVALID_SCOPE"})


class AttemptError(ValueError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


async def acknowledge_command(db, attempt_id, command_id, body):
    """Apply the same immutable receipt rules to HTTP delivery and crash recovery."""
    validate("response", body)
    if body["id"] != str(command_id):
        raise ProtocolError("INVALID_COMMAND", "Acknowledgement ID does not match")
    await locked_attempt(db, attempt_id)
    command = await db.scalar(
        select(ExecutionCommand).where(
            ExecutionCommand.id == command_id, ExecutionCommand.attempt_id == attempt_id
        )
    )
    if command is None:
        raise AttemptError("NOT_FOUND", "Command not found", 404)
    if command.result is not None:
        if command.result != body:
            raise AttemptError("COMMAND_CONFLICT", "Command acknowledgement is immutable")
        return {"duplicate": True}
    command.result = body
    command.status = "acknowledged" if body["ok"] else "failed"
    command.error = None if body["ok"] else body["error"]["message"]
    command.acknowledged_at = _utcnow_naive()
    return {"duplicate": False}


async def queue_command(db, attempt_id, name, payload, *, command_id=None):
    """Queue a declared command; both current and compatibility APIs use this."""
    command_id = command_id or uuid.uuid4()
    attempt = await locked_attempt(db, attempt_id)
    previous = await db.get(ExecutionCommand, command_id)
    if previous is not None:
        if (
            previous.attempt_id != attempt_id
            or previous.name != name
            or previous.payload != payload
        ):
            raise AttemptError("COMMAND_CONFLICT", "Command ID was already used")
        return {"id": str(previous.id), "status": previous.status, "duplicate": True}
    if attempt.status in TERMINAL:
        raise AttemptError("ATTEMPT_FINISHED", "Attempt has finished")
    if name != "abort" and attempt.manifest["capabilities"].get(name) not in ("native", "emulated"):
        raise ProtocolError("UNSUPPORTED_CAPABILITY", "Command is unavailable")
    schema = attempt.manifest.get("commands", {}).get(name, {}).get("input_schema")
    if name == "abort" and schema is None:
        schema = {"type": "object", "additionalProperties": False}
    if schema is None:
        raise ProtocolError("UNSUPPORTED_CAPABILITY", "Command has no declared input schema")
    validate_run(
        {**attempt.manifest, "config_schema": schema},
        {**attempt.run_spec, "config": payload, "required_capabilities": []},
    )
    command = ExecutionCommand(id=command_id, attempt_id=attempt_id, name=name, payload=payload)
    db.add(command)
    if name == "abort":
        attempt.status = "stopping"
    await db.flush()
    return {"id": str(command.id), "status": command.status, "duplicate": False}


async def create_attempt(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    task_id: uuid.UUID,
    manifest: dict,
    run_spec: dict,
    profile_id: uuid.UUID | None = None,
) -> ExecutionAttempt:
    validate_run(manifest, run_spec)
    _reject_inline_secrets(run_spec["config"], manifest["config_schema"])
    if run_spec.get("model"):
        _reject_inline_secrets(run_spec["model"], {})
    try:
        attempt_id = uuid.UUID(run_spec["attempt_id"])
    except ValueError:
        raise AttemptError("INVALID_SPEC", "Attempt ID must be a UUID", 422) from None
    if run_spec["workspace_id"] != str(workspace_id) or run_spec["task_id"] != str(task_id):
        raise AttemptError("INVALID_SCOPE", "RunSpec does not match the task scope", 422)
    async with db.begin_nested():
        task = await db.scalar(
            select(Task)
            .where(Task.id == task_id, Task.workspace_id == workspace_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if task is None:
            raise AttemptError("NOT_FOUND", "Task not found", 404)
        if profile_id is not None:
            profile = await db.scalar(
                select(HarnessProfile).where(
                    HarnessProfile.id == profile_id, HarnessProfile.workspace_id == workspace_id
                )
            )
            if profile is None:
                raise AttemptError("INVALID_SCOPE", "Profile is unavailable in this workspace", 422)
            if (profile.connector_id, profile.connector_version) != (
                manifest["id"],
                manifest["version"],
            ):
                raise AttemptError("INVALID_SPEC", "Profile and connector identity differ", 422)
        if task.current_attempt_id:
            previous = await db.scalar(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.id == task.current_attempt_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if previous and previous.status not in TERMINAL:
                raise AttemptError("ATTEMPT_ACTIVE", "Task already has an active attempt")
        number = (
            await db.scalar(
                select(func.max(ExecutionAttempt.number)).where(ExecutionAttempt.task_id == task_id)
            )
            or 0
        ) + 1
        attempt = ExecutionAttempt(
            id=attempt_id,
            workspace_id=workspace_id,
            task_id=task_id,
            number=number,
            profile_id=profile_id,
            connector_id=manifest["id"],
            connector_version=manifest["version"],
            protocol_version=manifest["protocol_version"],
            manifest=deepcopy(manifest),
            run_spec=deepcopy(run_spec),
            status="created",
            next_seq=0,
            completeness={
                "event_delivery": "open",
                "native_events": "unknown",
                "tools": "unknown",
                "reasoning": "unknown",
            },
        )
        db.add(attempt)
        await db.flush()
        task.current_attempt_id = attempt.id
        task.cost_usd = None
        task.token_usage = {}
        task.result_summary = None
        task.result_files = []
        await db.flush()
    return attempt


async def locked_attempt(db: AsyncSession, attempt_id: uuid.UUID) -> ExecutionAttempt:
    # Every mutation acquires task then attempt, including retries and controls.
    task_id = await db.scalar(
        select(ExecutionAttempt.task_id).where(ExecutionAttempt.id == attempt_id)
    )
    if task_id is None:
        raise AttemptError("NOT_FOUND", "Attempt not found", 404)
    task = await db.scalar(
        select(Task)
        .where(Task.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    attempt = await db.scalar(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None or attempt is None or task.workspace_id != attempt.workspace_id:
        raise AttemptError("INVALID_SCOPE", "Attempt scope is invalid", 404)
    if task.current_attempt_id != attempt.id:
        raise AttemptError("STALE_ATTEMPT", "A newer attempt owns this task")
    return attempt


async def ingest_events(db: AsyncSession, attempt_id: uuid.UUID, events: list[dict]) -> dict:
    if not events or len(events) > 128:
        raise AttemptError("INVALID_BATCH", "Event batch must contain 1 to 128 records", 422)
    for event in events:
        validate("event", event)
        encode_line(event)  # Includes strict JSON validation and the per-record byte cap.
        if event["attempt_id"] != str(attempt_id):
            raise AttemptError("INVALID_SCOPE", "Event does not belong to this attempt", 422)
    accepted = duplicates = 0
    async with db.begin_nested():
        attempt = await locked_attempt(db, attempt_id)
        for event in events:
            seq = event["seq"]
            event_key = hashlib.sha256(event["event_id"].encode()).hexdigest()
            if seq < attempt.next_seq:
                previous = await db.scalar(
                    select(ExecutionEvent).where(
                        ExecutionEvent.attempt_id == attempt_id, ExecutionEvent.seq == seq
                    )
                )
                if previous is None or previous.event_key != event_key or previous.payload != event:
                    raise AttemptError(
                        "EVENT_CONFLICT", "Sequence was already used by a different record"
                    )
                duplicates += 1
                continue
            if attempt.status in TERMINAL:
                raise AttemptError("ATTEMPT_FINISHED", "Attempt has finished")
            if seq != attempt.next_seq:
                raise AttemptError("EVENT_GAP", "Earlier events must be delivered first")
            existing = await db.scalar(
                select(ExecutionEvent.id).where(
                    ExecutionEvent.attempt_id == attempt_id, ExecutionEvent.event_key == event_key
                )
            )
            if existing:
                raise AttemptError(
                    "EVENT_CONFLICT", "Event ID was already used at another sequence"
                )
            if (seq == 0) != (event["event"] == "run.started"):
                raise AttemptError(
                    "INVALID_LIFECYCLE", "The first event must be the only run.started record", 422
                )
            now = _utcnow_naive()
            try:
                observed = (
                    datetime.fromisoformat(event["observed_at"].replace("Z", "+00:00")).replace(
                        tzinfo=None
                    )
                    if "observed_at" in event
                    else now
                )
            except ValueError:
                raise AttemptError(
                    "INVALID_TIMESTAMP", "Event observation time is invalid", 422
                ) from None
            if event["event"] == "run.started":
                if attempt.status != "stopping":
                    attempt.status = "running"
                attempt.started_at = observed
            if event["event"] == "run.finished":
                attempt.status, attempt.result, attempt.finished_at = (
                    event["data"]["status"],
                    deepcopy(event["data"]),
                    observed,
                )
                attempt.completeness = {**attempt.completeness, "event_delivery": "contiguous"}
                pending_commands = await db.scalars(
                    select(ExecutionCommand).where(
                        ExecutionCommand.attempt_id == attempt_id,
                        ExecutionCommand.status.in_(["queued", "delivered"]),
                    )
                )
                for command in pending_commands:
                    command.status, command.error = (
                        "unknown",
                        "Attempt ended before command acknowledgement",
                    )
            db.add(
                ExecutionEvent(
                    attempt_id=attempt_id,
                    event_key=event_key,
                    seq=seq,
                    event=event["event"],
                    payload=deepcopy(event),
                )
            )
            attempt.next_seq += 1
            attempt.last_heartbeat_at = now
            accepted += 1
            # Make earlier records visible to duplicate checks within this batch.
            await db.flush()
        next_seq = attempt.next_seq
    return {
        "accepted": accepted,
        "duplicates": duplicates,
        "next_seq": next_seq,
        "acked_through": next_seq - 1,
    }


async def mark_attempt_lost(db: AsyncSession, attempt_id: uuid.UUID, reason: str) -> None:
    async with db.begin_nested():
        attempt = await locked_attempt(db, attempt_id)
        if attempt.status in TERMINAL:
            return
        attempt.status, attempt.finished_at = "lost", _utcnow_naive()
        attempt.result = {
            "status": "failed",
            "summary": reason,
            "error": {"code": "TRANSPORT_LOST", "message": reason},
        }
        attempt.completeness = {**attempt.completeness, "event_delivery": "interrupted"}
        commands = await db.scalars(
            select(ExecutionCommand).where(
                ExecutionCommand.attempt_id == attempt_id,
                ExecutionCommand.status.in_(["queued", "delivered"]),
            )
        )
        for command in commands:
            command.status, command.error = (
                "unknown",
                "Runner stopped before command acknowledgement",
            )
