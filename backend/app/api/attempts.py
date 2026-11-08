"""Attempt reads use workspace JWTs; event writes require a scoped runner token."""

from __future__ import annotations

import json
import uuid
from contextlib import suppress

from evalhive_harness import ProtocolError
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.harnesses import SafeValidationRoute
from app.auth.dependencies import get_current_workspace, require_role
from app.auth.tokens import verify_runner_token
from app.database import get_db
from app.harnesses.attempts import (
    AttemptError,
    acknowledge_command,
    heartbeat_attempt,
    ingest_events,
    prepare_configuration,
    queue_command,
)
from app.models.execution_attempt import ExecutionAttempt, ExecutionCommand, ExecutionEvent
from app.models.task import Task
from app.models.workspace import Workspace

router = APIRouter(tags=["attempts"], route_class=SafeValidationRoute)
MAX_BATCH_BYTES = 4 * 1024 * 1024


async def require_runner(request, db, attempt_id):
    auth = request.headers.get("Authorization", "")
    if (
        not auth.startswith("Bearer ")
        or await verify_runner_token(db, plain=auth[7:], attempt_id=attempt_id) is None
    ):
        raise HTTPException(401, "Invalid or expired runner token")


async def bounded_json(request, limit=1024 * 1024):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "Request exceeds size limit")
        body.extend(chunk)
    try:
        return json.loads(
            body.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, "Invalid request JSON") from None


def attempt_error(error):
    if isinstance(error, AttemptError):
        return HTTPException(error.status, {"code": error.code, "message": str(error)})
    return HTTPException(422, {"code": error.code, "message": str(error)})


def attempt_dict(attempt: ExecutionAttempt) -> dict:
    return {
        "id": str(attempt.id),
        "task_id": str(attempt.task_id),
        "number": attempt.number,
        "profile_id": str(attempt.profile_id) if attempt.profile_id else None,
        "connector_id": attempt.connector_id,
        "connector_version": attempt.connector_version,
        "protocol_version": attempt.protocol_version,
        "status": attempt.status,
        "next_seq": attempt.next_seq,
        "result": attempt.result,
        "completeness": attempt.completeness,
        "capabilities": {
            **attempt.manifest.get("capabilities", {}),
            "abort": attempt.manifest.get("capabilities", {}).get("abort")
            if attempt.manifest.get("capabilities", {}).get("abort") in ("native", "emulated")
            else "emulated",
        },
        "commands": attempt.manifest.get("commands", {}),
        "created_at": attempt.created_at.isoformat(),
        "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
        "prepared_at": attempt.prepared_at.isoformat() if attempt.prepared_at else None,
        "finished_at": attempt.finished_at.isoformat() if attempt.finished_at else None,
        "last_heartbeat_at": attempt.last_heartbeat_at.isoformat(),
    }


async def scoped_attempt(db, workspace, attempt_id):
    attempt = await db.scalar(
        select(ExecutionAttempt).where(
            ExecutionAttempt.id == attempt_id, ExecutionAttempt.workspace_id == workspace.id
        )
    )
    if attempt is None:
        raise HTTPException(404, "Attempt not found")
    return attempt


@router.get("/api/tasks/{task_id}/attempts")
async def list_attempts(
    task_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    before_number: int | None = Query(None, ge=1),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    task = await db.scalar(
        select(Task.id).where(Task.id == task_id, Task.workspace_id == workspace.id)
    )
    if task is None:
        raise HTTPException(404, "Task not found")
    query = select(ExecutionAttempt).where(
        ExecutionAttempt.task_id == task_id, ExecutionAttempt.workspace_id == workspace.id
    )
    if before_number is not None:
        query = query.where(ExecutionAttempt.number < before_number)
    return [
        attempt_dict(attempt)
        for attempt in await db.scalars(query.order_by(ExecutionAttempt.number.desc()).limit(limit))
    ]


@router.get("/api/attempts/{attempt_id}")
async def read_attempt(
    attempt_id: uuid.UUID,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    return attempt_dict(await scoped_attempt(db, workspace, attempt_id))


@router.get("/api/attempts/{attempt_id}/events")
async def read_events(
    attempt_id: uuid.UUID,
    after_seq: int = Query(-1, ge=-1),
    limit: int = Query(100, ge=1, le=500),
    event_type: list[str] | None = Query(None, max_length=20),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    await scoped_attempt(db, workspace, attempt_id)
    query = (
        select(ExecutionEvent)
        .where(ExecutionEvent.attempt_id == attempt_id, ExecutionEvent.seq > after_seq)
        .order_by(ExecutionEvent.seq)
        .limit(limit)
    )
    if event_type:
        query = query.where(ExecutionEvent.payload["event"].astext.in_(event_type))
    rows = await db.scalars(query)
    return [row.payload for row in rows]


@router.post("/api/v1/attempts/{attempt_id}/configuration")
async def save_applied_configuration(
    attempt_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
):
    from app.harnesses.profiles import ProfileError

    await require_runner(request, db, attempt_id)
    body = await bounded_json(request)
    if not isinstance(body, dict) or set(body) != {"config"} or not isinstance(body["config"], dict):
        raise HTTPException(422, "Invalid applied configuration")
    try:
        receipt = await prepare_configuration(db, attempt_id, body["config"])
        await db.commit()
        return receipt
    except (AttemptError, ProtocolError) as error:
        await db.rollback()
        raise attempt_error(error) from None
    except ProfileError:
        await db.rollback()
        raise HTTPException(422, "Applied configuration contains inline credentials") from None


@router.post("/api/v1/attempts/{attempt_id}/events")
async def receive_events(
    attempt_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
):
    auth = request.headers.get("Authorization", "")
    if (
        not auth.startswith("Bearer ")
        or await verify_runner_token(db, plain=auth[7:], attempt_id=attempt_id) is None
    ):
        raise HTTPException(401, "Invalid or expired runner token")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BATCH_BYTES:
            raise HTTPException(413, "Event batch exceeds size limit")
        body.extend(chunk)
    try:
        payload = json.loads(
            body.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
        if (
            not isinstance(payload, dict)
            or set(payload) != {"events"}
            or not isinstance(payload["events"], list)
        ):
            raise ValueError()
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, "Invalid event batch") from None
    try:
        receipt = await ingest_events(db, attempt_id, payload["events"])
        await db.commit()
    except AttemptError as exc:
        await db.rollback()
        raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from None
    except ProtocolError as exc:
        await db.rollback()
        raise HTTPException(422, {"code": exc.code, "message": str(exc)}) from None
    if receipt["accepted"]:
        from app.harnesses.projection import publish_log_updates

        with suppress(Exception):
            await publish_log_updates(db, attempt_id, payload["events"])
    return receipt


@router.post("/api/attempts/{attempt_id}/commands", status_code=201)
async def enqueue_command(
    attempt_id: uuid.UUID,
    request: Request,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
    _role=Depends(require_role("owner", "admin", "member")),
):
    from app.benchmarks.task_routing import require_ordinary_task
    from app.models.task import Task

    attempt = await scoped_attempt(db, workspace, attempt_id)
    task = await db.get(Task, attempt.task_id)
    if task is not None:
        require_ordinary_task(task)
    body = await bounded_json(request)
    try:
        if (
            not isinstance(body, dict)
            or set(body) - {"id", "name", "payload"}
            or not isinstance(body.get("name"), str)
            or not 1 <= len(body["name"]) <= 100
            or not isinstance(body.get("payload", {}), dict)
        ):
            raise ValueError()
        command_id = uuid.UUID(body["id"]) if "id" in body else uuid.uuid4()
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "Invalid command") from None
    if request.state.role == "member" and body["name"] != "feedback":
        raise HTTPException(403, "This command requires owner or admin")
    try:
        receipt = await queue_command(
            db, attempt_id, body["name"], body.get("payload", {}), command_id=command_id
        )
        await db.commit()
        return receipt
    except (AttemptError, ProtocolError) as error:
        await db.rollback()
        raise attempt_error(error) from None


@router.get("/api/attempts/{attempt_id}/commands")
async def read_commands(
    attempt_id: uuid.UUID,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    await scoped_attempt(db, workspace, attempt_id)
    commands = await db.scalars(
        select(ExecutionCommand)
        .where(ExecutionCommand.attempt_id == attempt_id)
        .order_by(ExecutionCommand.created_at)
    )
    return [
        {
            "id": str(command.id),
            "name": command.name,
            "status": command.status,
            "result": command.result,
            "error": command.error,
            "request_id": command.payload.get("request_id"),
        }
        for command in commands
    ]


@router.post("/api/v1/attempts/{attempt_id}/heartbeat")
async def runner_heartbeat(
    attempt_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
):
    await require_runner(request, db, attempt_id)
    try:
        result = await heartbeat_attempt(db, attempt_id)
        await db.commit()
        return result
    except AttemptError as error:
        await db.rollback()
        raise attempt_error(error) from None


@router.post("/api/v1/attempts/{attempt_id}/commands/{command_id}/ack")
async def runner_ack(
    attempt_id: uuid.UUID,
    command_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    await require_runner(request, db, attempt_id)
    body = await bounded_json(request)
    try:
        receipt = await acknowledge_command(db, attempt_id, command_id, body)
        await db.commit()
        return receipt
    except (AttemptError, ProtocolError) as error:
        await db.rollback()
        raise attempt_error(error) from None
