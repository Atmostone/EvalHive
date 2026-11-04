"""Service token issuance/verification (per-task agent tokens, etc.)."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.service_token import ServiceToken


def hash_token(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def _utcnow_naive() -> datetime:
    """Tz-naive UTC datetime (DB columns are TIMESTAMP WITHOUT TIME ZONE)."""
    return datetime.now(UTC).replace(tzinfo=None)


async def issue_agent_token(
    db: AsyncSession,
    *,
    task_id: uuid.UUID,
    workspace_id: uuid.UUID,
    ttl_hours: int = 24,
) -> str:
    """Create a per-task agent token and return the plain value (single-use disclosure)."""
    plain = secrets.token_urlsafe(48)
    db.add(
        ServiceToken(
            kind="agent",
            token_hash=hash_token(plain),
            task_id=task_id,
            workspace_id=workspace_id,
            expires_at=_utcnow_naive() + timedelta(hours=ttl_hours),
        )
    )
    await db.flush()
    return plain


async def verify_agent_token(
    db: AsyncSession, *, plain: str, task_id: uuid.UUID
) -> ServiceToken | None:
    result = await db.execute(
        select(ServiceToken).where(
            ServiceToken.token_hash == hash_token(plain),
            ServiceToken.kind == "agent",
            ServiceToken.task_id == task_id,
        )
    )
    token = result.scalar_one_or_none()
    if not token:
        return None
    if token.expires_at and token.expires_at < _utcnow_naive():
        return None
    return token


async def issue_runner_token(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    task_id: uuid.UUID,
    workspace_id: uuid.UUID,
    ttl_hours: int = 24,
) -> str:
    from app.models.execution_attempt import ExecutionAttempt

    attempt = await db.get(ExecutionAttempt, attempt_id)
    if attempt is None or attempt.task_id != task_id or attempt.workspace_id != workspace_id:
        raise ValueError("Runner token scope does not match the attempt")
    plain = secrets.token_urlsafe(48)
    db.add(
        ServiceToken(
            kind="runner",
            token_hash=hash_token(plain),
            attempt_id=attempt_id,
            task_id=task_id,
            workspace_id=workspace_id,
            expires_at=_utcnow_naive() + timedelta(hours=ttl_hours),
        )
    )
    await db.flush()
    return plain


async def verify_runner_token(
    db: AsyncSession, *, plain: str, attempt_id: uuid.UUID
) -> ServiceToken | None:
    from app.models.execution_attempt import ExecutionAttempt
    from app.models.task import Task

    token = await db.scalar(
        select(ServiceToken)
        .join(ExecutionAttempt, ServiceToken.attempt_id == ExecutionAttempt.id)
        .join(Task, Task.id == ExecutionAttempt.task_id)
        .where(
            ServiceToken.token_hash == hash_token(plain),
            ServiceToken.kind == "runner",
            ServiceToken.attempt_id == attempt_id,
            ServiceToken.task_id == ExecutionAttempt.task_id,
            ServiceToken.workspace_id == ExecutionAttempt.workspace_id,
            Task.workspace_id == ExecutionAttempt.workspace_id,
        )
    )
    if token is None or (token.expires_at and token.expires_at < _utcnow_naive()):
        return None
    return token
