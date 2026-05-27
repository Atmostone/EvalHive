"""Trace Cleaner (E-06): turn a raw agent trajectory into a compact, judge-ready trace.

A raw trajectory is ~20-30K tokens even for a simple task: the per-spawn system
snapshot (`soul_md`, memory, tool/mcp lists), the full event history, and
megabyte-sized tool outputs in the logs. Feeding that to a trajectory judge
(E-07) is expensive and triggers "lost in the middle".

This module is the deterministic, **LLM-free** pre-processor described in §5.1
of EVALUATION_FRAMEWORK: from the durable sources (`agent_events` +
`agent_log_chunks` + `tasks`) it builds a `CleanedTrace` that keeps the original
task, the reasoning of each step and the tool calls/outputs (truncated), and
drops the system snapshot and noise events.

It produces the judge's *input* — it does not score anything and never writes
`trajectory_profile` (that slot belongs to E-07). Consistent with the rest of
`app.quality`, the cleaner never raises: on failure it returns a minimal trace
with an `error` field.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_log import AgentLogChunk
from app.models.event import AgentEvent
from app.models.task import Task

logger = logging.getLogger(__name__)

TRACE_SCHEMA_VERSION = 1

DEFAULT_TOOL_OUTPUT_TOKEN_CAP = 600
TOKEN_CAP_MIN = 50
TOKEN_CAP_MAX = 8000

# Reasoning/decision events — the agent's thinking, kept in full (the judge needs
# the "why" to assess optimality). Mapped to a step `kind`.
_REASONING_EVENTS = {
    "orchestrator_reasoning",
    "orchestrator_decision",
    "decomposition_decided",
}
# Agent lifecycle/progress signals — short, kept for context.
_AGENT_EVENTS = {
    "agent_progress",
    "agent_completed",
    "agent_failed",
    "agent_aborted",
    "task_retry",
    "task_timeout",
}
# Everything else is noise for trajectory judging: the system snapshot
# (`agent_spawned`), health pings, status churn, downstream eval events, etc.
# Anything not in the two allowlists above is dropped.

# Keys worth surfacing per event type, in priority order; falls back to a
# compact JSON dump of the whole `data` dict.
_EVENT_TEXT_KEYS = ("reasoning", "decision", "action", "message", "thought", "error", "reason")

_ERROR_RE = re.compile(r"\b(error|traceback|exception|failed|fatal)\b", re.IGNORECASE)


@dataclass
class TraceCleanerConfig:
    """Tunables for trace cleaning.

    tool_output_token_cap — truncate tool outputs longer than this many tokens.
    keep_tail_on_error — when set, tool steps that look like errors are kept in
        full (the bug is often in the ignored tail); for debugging runs.
    """

    tool_output_token_cap: int = DEFAULT_TOOL_OUTPUT_TOKEN_CAP
    keep_tail_on_error: bool = False


# --- token counting -------------------------------------------------------

_encoder = None
_encoder_loaded = False


def _get_encoder():
    """Lazy tiktoken singleton. Returns None if tiktoken is unavailable."""
    global _encoder, _encoder_loaded
    if not _encoder_loaded:
        _encoder_loaded = True
        try:
            import tiktoken

            _encoder = tiktoken.get_encoding("cl100k_base")
        except Exception as e:  # pragma: no cover - environment-dependent
            logger.warning(f"tiktoken unavailable, falling back to char/4 estimate: {e}")
            _encoder = None
    return _encoder


def _count_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _get_encoder()
    if enc is None:
        return len(text) // 4  # rough estimate when tiktoken is missing
    return len(enc.encode(text))


def _truncate_to_tokens(text: str, cap: int) -> tuple[str, int]:
    """Keep the first `cap` tokens of `text`. Returns (head, dropped_token_count)."""
    if not text:
        return "", 0
    enc = _get_encoder()
    if enc is None:
        # char/4 estimate: keep first cap*4 chars.
        char_cap = cap * 4
        if len(text) <= char_cap:
            return text, 0
        return text[:char_cap], (len(text) - char_cap) // 4
    tokens = enc.encode(text)
    if len(tokens) <= cap:
        return text, 0
    head = enc.decode(tokens[:cap])
    return head, len(tokens) - cap


# --- step extraction ------------------------------------------------------


def _ts(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def _event_text(data: dict) -> str:
    """Render a readable line from an event's `data` dict."""
    if not isinstance(data, dict) or not data:
        return ""
    parts = [str(data[k]).strip() for k in _EVENT_TEXT_KEYS if data.get(k)]
    if parts:
        return "\n".join(p for p in parts if p)
    # Unknown shape — compact dump so nothing meaningful is silently lost.
    try:
        return json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        return str(data)


def _event_step(ev) -> dict | None:
    event_type = getattr(ev, "event_type", None)
    if event_type in _REASONING_EVENTS:
        kind = "reasoning"
    elif event_type in _AGENT_EVENTS:
        kind = "agent"
    else:
        return None
    text = _event_text(getattr(ev, "data", None) or {})
    return {
        "kind": kind,
        "event_type": event_type,
        "tool_name": None,
        "content": text,
        "_ts": _ts(getattr(ev, "created_at", None)),
        "_order": 0,
    }


def _chunk_step(chunk, order: int) -> dict:
    return {
        "kind": "tool",
        "event_type": None,
        "tool_name": getattr(chunk, "tool_name", None),
        "content": getattr(chunk, "content", "") or "",
        "_ts": _ts(getattr(chunk, "created_at", None)),
        "_order": order,
    }


def clean_trajectory(
    task,
    events,
    log_chunks,
    *,
    config: TraceCleanerConfig | None = None,
) -> dict:
    """Build a CleanedTrace from in-memory trajectory inputs.

    Pure and deterministic: filters noise events, drops the system snapshot,
    truncates long tool outputs, and reports token savings. Never raises.
    """
    config = config or TraceCleanerConfig()
    cap = max(TOKEN_CAP_MIN, min(TOKEN_CAP_MAX, int(config.tool_output_token_cap)))
    task_failed = getattr(task, "status", None) == "failed"

    try:
        events = list(events or [])
        log_chunks = list(log_chunks or [])

        # Baseline: what a naive trace would cost — system snapshot + every
        # event payload + every (untruncated) tool output.
        original_tokens = 0
        for ev in events:
            if getattr(ev, "event_type", None) == "agent_spawned":
                snap = getattr(ev, "data", None) or {}
                original_tokens += _count_tokens(json.dumps(snap, ensure_ascii=False, default=str))
            else:
                original_tokens += _count_tokens(_event_text(getattr(ev, "data", None) or {}))
        for chunk in log_chunks:
            original_tokens += _count_tokens(getattr(chunk, "content", "") or "")

        events_dropped = sum(
            1 for ev in events if _event_step(ev) is None
        )

        raw_steps = [s for s in (_event_step(ev) for ev in events) if s is not None]
        for i, chunk in enumerate(log_chunks):
            raw_steps.append(_chunk_step(chunk, order=i))

        # Chronological merge: dated items ascending, undated (archive) last in
        # their original order (stable sort).
        raw_steps.sort(key=lambda s: (s["_ts"] is None, s["_ts"] or 0.0, s["_order"]))

        steps: list[dict] = []
        steps_truncated = 0
        for seq, s in enumerate(raw_steps):
            content = s["content"]
            original = _count_tokens(content)
            truncated = False
            kept = original

            if s["kind"] == "tool" and original > cap:
                is_error = bool(_ERROR_RE.search(content)) or task_failed
                if config.keep_tail_on_error and is_error:
                    pass  # keep full content — debugging the ignored tail
                else:
                    head, dropped = _truncate_to_tokens(content, cap)
                    content = f"{head}\n…[truncated {dropped} tokens]…"
                    truncated = True
                    kept = cap
                    steps_truncated += 1

            steps.append(
                {
                    "seq": seq,
                    "kind": s["kind"],
                    "tool_name": s["tool_name"],
                    "content": content,
                    "truncated": truncated,
                    "original_tokens": original,
                    "kept_tokens": kept,
                }
            )

        task_block = {
            "id": str(getattr(task, "id", "") or ""),
            "title": getattr(task, "title", None),
            "description": getattr(task, "description", None),
        }

        cleaned_tokens = _count_tokens(
            _event_text(task_block) + "\n".join(s["content"] for s in steps)
        )
        savings = original_tokens - cleaned_tokens
        savings_pct = round(savings / original_tokens * 100, 1) if original_tokens else 0.0

        return {
            "schema_version": TRACE_SCHEMA_VERSION,
            "task": task_block,
            "steps": steps,
            "stats": {
                "original_tokens": original_tokens,
                "cleaned_tokens": cleaned_tokens,
                "savings_tokens": savings,
                "savings_pct": savings_pct,
                "steps_total": len(steps),
                "steps_truncated": steps_truncated,
                "events_dropped": events_dropped,
            },
            "config": {
                "tool_output_token_cap": cap,
                "keep_tail_on_error": config.keep_tail_on_error,
            },
            "generated_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:  # never break the caller — same contract as the judge
        logger.warning(f"trace cleaning failed for task {getattr(task, 'id', '?')}: {e}")
        return {
            "schema_version": TRACE_SCHEMA_VERSION,
            "task": {"id": str(getattr(task, "id", "") or "")},
            "steps": [],
            "stats": {
                "original_tokens": 0,
                "cleaned_tokens": 0,
                "savings_tokens": 0,
                "savings_pct": 0.0,
                "steps_total": 0,
                "steps_truncated": 0,
                "events_dropped": 0,
            },
            "config": {"tool_output_token_cap": cap, "keep_tail_on_error": config.keep_tail_on_error},
            "generated_at": datetime.utcnow().isoformat(),
            "error": str(e),
        }


async def _load_log_chunks(db: AsyncSession, task: Task) -> list:
    """Load a task's log chunks from Postgres, or the MinIO archive after
    compaction (mirrors api/agent_logs.list_log_chunks). The JSON-lines archive
    preserves `tool_name`; legacy plain-text archives lose it (degrade gracefully).
    `created_at` is always absent from the archive."""
    if task.log_archive_s3_path:
        try:
            from app.storage.minio_client import decode_log_archive, read_log_archive

            blob = read_log_archive(task.log_archive_s3_path).decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"reading log archive {task.log_archive_s3_path} failed: {e}")
            blob = ""
        return [
            AgentLogChunk(task_id=task.id, chunk_seq=i, content=d["content"], tool_name=d.get("tool_name"))
            for i, d in enumerate(decode_log_archive(blob))
        ]

    return (
        await db.execute(
            select(AgentLogChunk)
            .where(AgentLogChunk.task_id == task.id)
            .order_by(AgentLogChunk.chunk_seq)
        )
    ).scalars().all()


async def build_cleaned_trace(
    db: AsyncSession, task: Task, *, config: TraceCleanerConfig | None = None
) -> dict:
    """Load a task's trajectory (events + log chunks) and clean it. The consumer
    is the trace preview endpoint and, later, the trajectory judge (E-07)."""
    events = (
        await db.execute(
            select(AgentEvent)
            .where(AgentEvent.task_id == task.id)
            .order_by(AgentEvent.created_at)
        )
    ).scalars().all()
    log_chunks = await _load_log_chunks(db, task)
    return clean_trajectory(task, events, log_chunks, config=config)
