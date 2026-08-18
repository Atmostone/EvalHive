"""Agent entrypoint: runs the agent task and reports result via webhook."""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("entrypoint")


_FAILED_QUEUE_PATH = "/tmp/failed_webhooks.json"


def _persist_failed(payload: dict, error: str) -> None:
    """Append a webhook payload that we failed to deliver to a local file."""
    try:
        existing: list = []
        if os.path.exists(_FAILED_QUEUE_PATH):
            with open(_FAILED_QUEUE_PATH) as f:
                existing = json.load(f) or []
        existing.append({"payload": payload, "error": error,
                         "at": datetime.now(timezone.utc).isoformat()})
        with open(_FAILED_QUEUE_PATH, "w") as f:
            json.dump(existing, f)
    except Exception as e:  # pragma: no cover — best-effort
        logger.warning(f"failed to persist failed-webhook record: {e}")


async def report_webhook(result: dict, *, idempotency_key: str | None = None) -> bool:
    """Send result to orchestrator via webhook with retry+backoff. Returns True on 2xx."""
    webhook_url = os.environ.get("WEBHOOK_URL")
    if not webhook_url:
        logger.error("WEBHOOK_URL not set, cannot report")
        return False

    result.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    if idempotency_key is None:
        idempotency_key = uuid.uuid4().hex
    result["idempotency_key"] = idempotency_key

    token = os.environ.get("SPAWNHIVE_AGENT_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    delays = (2, 4, 8)
    last_err: str | None = None
    for attempt, delay in enumerate((0,) + delays, start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(webhook_url, json=result, headers=headers)
            if 200 <= resp.status_code < 300:
                logger.info(f"Webhook delivered (attempt {attempt}, status {resp.status_code})")
                return True
            last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            logger.warning(f"Webhook attempt {attempt} failed: {last_err}")
            if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
                # Client errors that won't resolve via retry — give up.
                break
        except Exception as e:
            last_err = str(e)
            logger.warning(f"Webhook attempt {attempt} raised: {e}")

    _persist_failed(result, last_err or "unknown")
    return False


async def main():
    from agent import _failure_facts, run_agent
    from feedback_server import start_feedback_server

    task_id = os.environ.get("TASK_ID", "unknown")
    logger.info(f"Starting agent for task {task_id}")

    server_task = await start_feedback_server()

    try:
        result = await run_agent()
    except Exception as e:
        logger.error(f"Agent crashed: {e}", exc_info=True)
        result = {
            "task_id": task_id,
            "event": "failed",
            "data": {"error": f"Agent crashed: {e}", "failure": _failure_facts("crash", e)},
        }

    server_task.cancel()
    try:
        await server_task
    except (asyncio.CancelledError, Exception):
        pass

    await report_webhook(result)
    logger.info("Agent shutting down")


if __name__ == "__main__":
    asyncio.run(main())
