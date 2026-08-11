import asyncio
import json
import logging
import os
import uuid
from typing import Any

from fastapi import WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import AgentEvent

logger = logging.getLogger(__name__)

# Global WebSocket event client registry
_event_clients: dict[WebSocket, dict] = {}
_lock = asyncio.Lock()

# Redis pub/sub adapter — optional. If REDIS_URL is set, log_event publishes to
# a channel and a subscriber task in this process delivers to local WS clients.
# If unset, broadcast happens in-process only (single-replica fallback).
EVENTS_CHANNEL = "spawnhive.events"
_redis_publisher: Any = None
_redis_subscriber_task: asyncio.Task | None = None
_redis_url: str | None = None
# Is the subscriber task actually consuming right now? The Redis round-trip is
# what delivers events back to *our own* WS clients, so when this is False the
# publish path is not a delivery path and has to fall back to a local broadcast.
_subscriber_live = False

# Poll interval for the subscriber. Deliberately bounded rather than a blocking
# listen(): redis-py 8.x defaults the connection's socket_timeout to 5s, so a
# blocking read raises TimeoutError on any idle gap — which is the normal state
# of this channel. get_message() with an explicit timeout returns None instead.
_SUBSCRIBER_POLL_SECONDS = 1.0
_SUBSCRIBER_BACKOFF_SECONDS = (1, 2, 5, 10, 30)


def _get_redis_url() -> str | None:
    return os.environ.get("REDIS_URL")


async def _consume() -> None:
    """Relay Redis events to local WS clients, resubscribing for as long as the process lives.

    A dropped subscription is the difference between a live UI and a silent one,
    so it is never terminal here: any failure is logged, backed off, and retried.
    """
    global _subscriber_live
    attempt = 0
    while True:
        sub = _redis_publisher.pubsub()
        try:
            await sub.subscribe(EVENTS_CHANNEL)
            _subscriber_live = True
            attempt = 0
            logger.info(f"events: subscribed to Redis channel {EVENTS_CHANNEL}")
            while True:
                message = await sub.get_message(
                    ignore_subscribe_messages=True, timeout=_SUBSCRIBER_POLL_SECONDS
                )
                if message is None:  # idle poll — no traffic, not a failure
                    continue
                if message.get("type") != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
                except Exception:
                    continue
                await _broadcast_event_local(payload)
        except asyncio.CancelledError:
            _subscriber_live = False
            await _close_pubsub(sub)
            raise
        except Exception as e:
            _subscriber_live = False
            await _close_pubsub(sub)
            delay = _SUBSCRIBER_BACKOFF_SECONDS[
                min(attempt, len(_SUBSCRIBER_BACKOFF_SECONDS) - 1)
            ]
            attempt += 1
            logger.warning(f"Redis subscriber dropped ({e}); resubscribing in {delay}s")
            await asyncio.sleep(delay)


async def _close_pubsub(sub: Any) -> None:
    """Release a pubsub connection, ignoring whatever state it died in."""
    try:
        await sub.aclose()
    except Exception:
        pass


async def start_event_subscriber() -> None:
    """If REDIS_URL is configured, subscribe to the events channel and fan out to local WS clients."""
    global _redis_publisher, _redis_subscriber_task, _redis_url
    _redis_url = _get_redis_url()
    if not _redis_url:
        return
    try:
        import redis.asyncio as aioredis
    except ImportError:  # pragma: no cover
        logger.warning("REDIS_URL set but `redis` package not installed; falling back to local-only broadcast")
        _redis_url = None
        return

    try:
        _redis_publisher = aioredis.from_url(_redis_url, decode_responses=True)
        await _redis_publisher.ping()
    except Exception as e:
        logger.warning(f"Redis ping failed ({e}); falling back to local-only broadcast")
        _redis_publisher = None
        _redis_url = None
        return

    _redis_subscriber_task = asyncio.create_task(_consume())


async def stop_event_subscriber() -> None:
    global _redis_publisher, _redis_subscriber_task, _subscriber_live
    _subscriber_live = False
    if _redis_subscriber_task:
        _redis_subscriber_task.cancel()
        try:
            await _redis_subscriber_task
        except (asyncio.CancelledError, Exception):
            pass
        _redis_subscriber_task = None
    if _redis_publisher:
        try:
            await _redis_publisher.aclose()
        except Exception:
            pass
        _redis_publisher = None


async def register_event_client(ws: WebSocket, filters: dict | None = None):
    async with _lock:
        _event_clients[ws] = filters or {}


async def unregister_event_client(ws: WebSocket):
    async with _lock:
        _event_clients.pop(ws, None)


async def update_event_client_filters(ws: WebSocket, filters: dict):
    async with _lock:
        if ws in _event_clients:
            _event_clients[ws] = filters


def _event_matches_filter(event_dict: dict, filters: dict) -> bool:
    if not filters:
        return True
    msg_kind = event_dict.get("_kind", "event")
    expected_kind = filters.get("_kind", "event")
    if msg_kind != expected_kind:
        return False
    if filters.get("task_id") and str(event_dict.get("task_id") or "") != filters["task_id"]:
        return False
    if filters.get("source") and event_dict.get("source") != filters["source"]:
        return False
    if filters.get("event_type") and event_dict.get("event_type") != filters["event_type"]:
        return False
    if (
        filters.get("agent_container_id")
        and event_dict.get("agent_container_id") != filters["agent_container_id"]
    ):
        return False
    if (
        filters.get("workspace_id")
        and str(event_dict.get("workspace_id") or "") != filters["workspace_id"]
    ):
        return False
    return True


async def _broadcast_event_local(event_dict: dict):
    """Fan out a single event to every locally-registered WS client matching the filter."""
    wire_type = event_dict.get("_kind", "event")
    message = json.dumps({"type": wire_type, **event_dict})
    disconnected = []

    async with _lock:
        clients = list(_event_clients.items())

    for ws, filters in clients:
        if _event_matches_filter(event_dict, filters):
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)

    if disconnected:
        async with _lock:
            for ws in disconnected:
                _event_clients.pop(ws, None)


async def _broadcast_event(event_dict: dict):
    """Publish to Redis if configured (fans out across replicas), else local-only.

    Publishing only reaches our own WS clients by coming back through the
    subscriber, so it replaces the local broadcast solely while that subscriber
    is consuming. While it is down we broadcast locally instead — replicas lose
    each other's events, but a client still sees the ones raised in its process.
    """
    if _redis_publisher is not None:
        try:
            await _redis_publisher.publish(EVENTS_CHANNEL, json.dumps(event_dict, default=str))
            if _subscriber_live:
                return
        except Exception as e:
            logger.warning(f"Redis publish failed, falling back to local broadcast: {e}")
    await _broadcast_event_local(event_dict)


async def log_event(
    db: AsyncSession,
    event_type: str,
    source: str,
    data: dict | None = None,
    task_id: uuid.UUID | str | None = None,
    agent_container_id: str | None = None,
    workspace_id: uuid.UUID | str | None = None,
    *,
    commit: bool = True,
) -> AgentEvent:
    """Insert an event row.

    When ``commit=True`` (default) the row is committed and broadcast immediately.
    When ``commit=False`` the row is only flushed; the caller owns the
    transaction and is responsible for committing — broadcast is skipped because
    the row may still be rolled back. Use commit=False to bundle event-writes
    inside a larger atomic operation (see webhook idempotency handler).
    """
    if isinstance(task_id, str):
        task_id = uuid.UUID(task_id)
    if isinstance(workspace_id, str):
        workspace_id = uuid.UUID(workspace_id)

    # If workspace_id wasn't passed but task_id was, derive it from the task.
    if workspace_id is None and task_id is not None:
        from app.models.task import Task

        task = await db.get(Task, task_id)
        if task is not None:
            workspace_id = task.workspace_id

    if workspace_id is None:
        raise ValueError("log_event requires workspace_id (directly or via task_id)")

    event = AgentEvent(
        task_id=task_id,
        agent_container_id=agent_container_id,
        event_type=event_type,
        source=source,
        data=data or {},
        workspace_id=workspace_id,
    )
    db.add(event)
    if not commit:
        # Caller owns the transaction; flush so id/created_at are populated.
        await db.flush()
        return event

    await db.commit()
    await db.refresh(event)

    event_dict = {
        "id": event.id,
        "task_id": str(event.task_id) if event.task_id else None,
        "agent_container_id": event.agent_container_id,
        "event_type": event.event_type,
        "source": event.source,
        "data": event.data,
        "workspace_id": str(event.workspace_id),
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }
    try:
        await _broadcast_event(event_dict)
    except Exception as e:
        logger.warning(f"Event broadcast failed: {e}")

    await _notify_event(event_type, data or {}, workspace_id)

    return event


async def _notify_event(event_type: str, data: dict, workspace_id: uuid.UUID) -> None:
    """Hook the Notifier plugin. NoopNotifier is the default — Slack/email/etc swap in via env."""
    try:
        from app.plugins.notifier import get_notifier

        await get_notifier().notify(event_type, data, workspace_id)
    except Exception as e:
        logger.warning(f"Notifier dispatch failed: {e}")


async def broadcast_log_chunk(chunk_dict: dict) -> None:
    """Broadcast a log chunk via the same fan-out as agent_events.

    Subscribers register with `_kind='log_chunk'` filter on `/ws/tasks/{task_id}/log`.
    Default-kind subscribers (`/ws/events`, `/ws/agents/{cid}`) won't see it.
    """
    payload = {**chunk_dict, "_kind": "log_chunk"}
    try:
        await _broadcast_event(payload)
    except Exception as e:
        logger.warning(f"Log chunk broadcast failed: {e}")


async def broadcast_committed_event(event: AgentEvent) -> None:
    """Broadcast an event that was inserted with commit=False and later committed by caller."""
    event_dict = {
        "id": event.id,
        "task_id": str(event.task_id) if event.task_id else None,
        "agent_container_id": event.agent_container_id,
        "event_type": event.event_type,
        "source": event.source,
        "data": event.data,
        "workspace_id": str(event.workspace_id),
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }
    try:
        await _broadcast_event(event_dict)
    except Exception as e:
        logger.warning(f"Event broadcast failed: {e}")
    await _notify_event(event.event_type, event.data or {}, event.workspace_id)
