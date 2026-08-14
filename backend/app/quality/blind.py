"""What an annotator was actually shown — the basis of the blind flag (SPA-85).

A blindness flag the client asserts is worth nothing: the UI could show the
judge's scores, let the annotator read them, and still submit
``blind_to_judge: true``. So the client does not get to assert it. Every endpoint
that serves a judge score records a **reveal** for (user, task); the annotation
write then derives blindness from that ledger instead of from the request body.

That makes the property the flag claims literally true: an annotator who was
served the judge's opinion for this run cannot be recorded as blind to it, and
the decision is made by *how they fetched*, before any rating exists — which is
also what makes it irreversible for the session.

Reveals live in Redis with a long TTL and **fail safe**: if the ledger cannot be
read, the answer is «revealed», so a lost record can only ever under-claim
blindness, never over-claim it.

Scope, stated plainly: only blindness to the **judge** is certified. `model_used`
is served by a dozen surfaces outside annotation (task detail, the data lake,
analytics, experiment results) and instrumenting a subset would produce a claim
we cannot stand behind, so `blind_to_model` is never derived from the UI — it is
declarable only by a scripted annotator that owns its own protocol.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Long enough that an annotation campaign cannot outlive its own evidence.
REVEAL_TTL_SECONDS = 90 * 24 * 3600
_KEY_PREFIX = "spawnhive:judge_reveal"


def _key(user_id, task_id) -> str:
    return f"{_KEY_PREFIX}:{user_id}:{task_id}"


def _client():
    """The shared Redis client, or ``None`` when Redis is not configured."""
    from app.utils import events

    return events._redis_publisher


async def mark_judge_revealed(user_id, task_ids) -> None:
    """Record that this user has been served the judge's opinion on these tasks.

    Best-effort and never raises: a failure here must not break a read endpoint.
    It can only cost a later annotation its blind claim, which is the safe way to
    be wrong."""
    client = _client()
    ids = [t for t in task_ids if t]
    if client is None or not ids or user_id is None:
        return
    try:
        pipe = client.pipeline()
        for task_id in ids:
            pipe.set(_key(user_id, task_id), "1", ex=REVEAL_TTL_SECONDS)
        await pipe.execute()
    except Exception as e:  # noqa: BLE001 — a read must not fail over bookkeeping
        logger.warning(f"blind: could not record judge reveal: {e}")


async def judge_was_revealed(user_id, task_id) -> bool:
    """Has this user been served the judge's opinion on this task?

    ``True`` whenever the answer is unknown — no Redis, a read error, no user —
    so an unverifiable blind claim is refused rather than granted."""
    client = _client()
    if client is None or user_id is None:
        return True
    try:
        return bool(await client.exists(_key(user_id, task_id)))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"blind: could not read judge reveal, assuming revealed: {e}")
        return True
