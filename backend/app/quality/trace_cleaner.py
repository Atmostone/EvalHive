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

# v2: tool-call arguments per step, and joined multi-part outputs (SPA-86)
TRACE_SCHEMA_VERSION = 2

DEFAULT_TOOL_OUTPUT_TOKEN_CAP = 600
# Arguments get a cap of their own: a file body can arrive as a parameter, and the
# names of the parameters matter more to the judge than their full contents.
DEFAULT_TOOL_ARGS_TOKEN_CAP = 400
TOKEN_CAP_MIN = 50
# The ceiling catches typos; it is not meant to bound the model's context. With a
# 1M-context judge a five-figure cap is an ordinary request.
TOKEN_CAP_MAX = 50000
# Both caps accept 0 as «do not truncate at all» (SPA-86).
TOKEN_CAP_OFF = 0

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

    tool_output_token_cap — truncate tool outputs longer than this many tokens;
        ``0`` disables output truncation entirely.
    tool_args_token_cap — the same, for the tool call's arguments; ``0`` disables.
    keep_tail_on_error — when set, tool steps that look like errors are kept in
        full (the bug is often in the ignored tail); for debugging runs.
    """

    tool_output_token_cap: int = DEFAULT_TOOL_OUTPUT_TOKEN_CAP
    tool_args_token_cap: int = DEFAULT_TOOL_ARGS_TOKEN_CAP
    keep_tail_on_error: bool = False


def effective_cap(raw, default: int) -> int:
    """Resolve a configured token cap. ``0`` (or any non-positive value) means «no
    truncation» and is passed through as 0; anything else is clamped into the sane
    band. A garbage value falls back to the default rather than silently disabling
    the cap — off has to be asked for explicitly."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return TOKEN_CAP_OFF
    return max(TOKEN_CAP_MIN, min(TOKEN_CAP_MAX, value))


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
        "arguments": None,
        "arguments_truncated": False,
        "content": text,
        "_ts": _ts(getattr(ev, "created_at", None)),
        "_order": 0,
    }


def render_arguments(args: dict | None) -> str:
    """The judge-facing rendering of a tool call's arguments. Shared with the
    serializer so what is measured against the cap is what is actually sent."""
    if not args:
        return ""
    try:
        return json.dumps(args, ensure_ascii=False, default=str)
    except Exception:
        return str(args)


def _clip_str_values(args: dict, char_limit: int) -> tuple[dict, bool]:
    """Shorten every string value longer than `char_limit`, recursively."""
    truncated = False

    def _clip(v):
        nonlocal truncated
        if isinstance(v, str) and len(v) > char_limit:
            truncated = True
            return f"{v[:char_limit]}…[truncated {len(v) - char_limit} chars]…"
        if isinstance(v, dict):
            return {k: _clip(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_clip(x) for x in v]
        return v

    return {k: _clip(v) for k, v in args.items()}, truncated


def _truncate_arguments(args: dict | None, cap: int) -> tuple[dict | None, bool]:
    """Fit a tool call's arguments into `cap` tokens by shortening long string
    values. **Keys are never dropped** — which parameters were passed is the signal
    the process judge is missing, and a truncated value still answers «did it pass a
    path at all», where an absent key answers nothing. `cap <= 0` disables the cap.
    Returns (arguments, was_truncated)."""
    if not args or cap <= 0:
        return args, False
    if _count_tokens(render_arguments(args)) <= cap:
        return args, False

    limit = max(16, cap * 4)  # tokens → rough char budget
    out, truncated = args, False
    for _ in range(12):
        out, truncated = _clip_str_values(args, limit)
        if _count_tokens(render_arguments(out)) <= cap:
            return out, truncated
        limit = max(8, limit // 2)
    # Enough keys to blow the cap on their names alone. Keep them all and say so,
    # rather than deciding which parameter the judge is allowed to know about.
    return out, True


def _chunk_attr(chunk, name, default=None):
    """Read a field off a chunk that may be an ORM row or a decoded archive dict."""
    value = chunk.get(name, default) if isinstance(chunk, dict) else getattr(chunk, name, default)
    return default if value is None else value


def _join_tool_call_parts(log_chunks) -> list[dict]:
    """Re-join the parts of one tool output into one call.

    An output over the agent's transport cap arrives as several consecutive rows
    sharing a `tool_call_id`. Left alone, each becomes its own step — separately
    truncated, separately counted by the loop detector — so one call reads as three.
    Only *consecutive* rows are merged: a provider that reuses ids across turns must
    not fuse two genuinely different calls. Rows with no call id (pre-SPA-86 rows,
    legacy archives) keep one step per chunk."""
    joined: list[dict] = []
    for chunk in log_chunks:
        call_id = _chunk_attr(chunk, "tool_call_id")
        content = _chunk_attr(chunk, "content", "") or ""
        prev = joined[-1] if joined else None
        if call_id and prev is not None and prev["tool_call_id"] == call_id:
            prev["parts"].append((_chunk_attr(chunk, "part_index", 0), content))
            if prev["arguments"] is None:
                prev["arguments"] = _chunk_attr(chunk, "arguments")
            prev["arguments_truncated"] = prev["arguments_truncated"] or bool(
                _chunk_attr(chunk, "arguments_truncated", False)
            )
            continue
        joined.append(
            {
                "tool_name": _chunk_attr(chunk, "tool_name"),
                "tool_call_id": call_id,
                "arguments": _chunk_attr(chunk, "arguments"),
                "arguments_truncated": bool(_chunk_attr(chunk, "arguments_truncated", False)),
                "created_at": _chunk_attr(chunk, "created_at"),
                "parts": [(_chunk_attr(chunk, "part_index", 0), content)],
            }
        )
    for entry in joined:
        entry["content"] = "".join(c for _, c in sorted(entry["parts"], key=lambda p: p[0]))
        del entry["parts"]
    return joined


def _chunk_step(chunk: dict, order: int) -> dict:
    return {
        "kind": "tool",
        "event_type": None,
        "tool_name": chunk.get("tool_name"),
        "arguments": chunk.get("arguments"),
        "arguments_truncated": bool(chunk.get("arguments_truncated")),
        "content": chunk.get("content", "") or "",
        "_ts": _ts(chunk.get("created_at")),
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
    cap = effective_cap(config.tool_output_token_cap, DEFAULT_TOOL_OUTPUT_TOKEN_CAP)
    args_cap = effective_cap(config.tool_args_token_cap, DEFAULT_TOOL_ARGS_TOKEN_CAP)
    task_failed = getattr(task, "status", None) == "failed"

    try:
        events = list(events or [])
        # Parts of one split output are re-joined here, before anything is counted:
        # otherwise a single call is capped N times and counted N times.
        calls = _join_tool_call_parts(list(log_chunks or []))

        # Baseline: what a naive trace would cost — system snapshot + every
        # event payload + every (untruncated) tool call and output.
        original_tokens = 0
        for ev in events:
            if getattr(ev, "event_type", None) == "agent_spawned":
                snap = getattr(ev, "data", None) or {}
                original_tokens += _count_tokens(json.dumps(snap, ensure_ascii=False, default=str))
            else:
                original_tokens += _count_tokens(_event_text(getattr(ev, "data", None) or {}))
        for call in calls:
            original_tokens += _count_tokens(call.get("content", "") or "")
            original_tokens += _count_tokens(render_arguments(call.get("arguments")))

        events_dropped = sum(
            1 for ev in events if _event_step(ev) is None
        )

        raw_steps = [s for s in (_event_step(ev) for ev in events) if s is not None]
        for i, call in enumerate(calls):
            raw_steps.append(_chunk_step(call, order=i))

        # Chronological merge: dated items ascending, undated (archive) last in
        # their original order (stable sort).
        raw_steps.sort(key=lambda s: (s["_ts"] is None, s["_ts"] or 0.0, s["_order"]))

        steps: list[dict] = []
        steps_truncated = 0
        steps_args_truncated = 0
        for seq, s in enumerate(raw_steps):
            content = s["content"]
            original = _count_tokens(content)
            truncated = False
            kept = original

            arguments, args_truncated = _truncate_arguments(s.get("arguments"), args_cap)
            # The agent may already have clipped an oversized value in transit; that
            # truncation is a fact about this step whether or not the cap fires here.
            args_truncated = bool(args_truncated or s.get("arguments_truncated"))
            if args_truncated:
                steps_args_truncated += 1

            if s["kind"] == "tool" and cap and original > cap:
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
                    "arguments": arguments,
                    "arguments_truncated": args_truncated,
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
            _event_text(task_block)
            + "\n".join(render_arguments(s["arguments"]) + s["content"] for s in steps)
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
                "steps_args_truncated": steps_args_truncated,
                "events_dropped": events_dropped,
            },
            "config": {
                "tool_output_token_cap": cap,
                "tool_args_token_cap": args_cap,
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
                "steps_args_truncated": 0,
                "events_dropped": 0,
            },
            "config": {
                "tool_output_token_cap": cap,
                "tool_args_token_cap": args_cap,
                "keep_tail_on_error": config.keep_tail_on_error,
            },
            "generated_at": datetime.utcnow().isoformat(),
            "error": str(e),
        }


async def _load_log_chunks(db: AsyncSession, task: Task) -> list:
    """Load a task's log chunks from Postgres, or the MinIO archive after
    compaction (mirrors api/agent_logs.list_log_chunks). The JSON-lines archive
    preserves the tool call — name, arguments and part identity; legacy plain-text
    archives lose them (degrade gracefully). `created_at` is always absent from the
    archive."""
    if task.log_archive_s3_path:
        try:
            from app.storage.minio_client import decode_log_archive, read_log_archive

            blob = read_log_archive(task.log_archive_s3_path).decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"reading log archive {task.log_archive_s3_path} failed: {e}")
            blob = ""
        return [
            AgentLogChunk(
                task_id=task.id,
                chunk_seq=i,
                content=d["content"],
                tool_name=d.get("tool_name"),
                arguments=d.get("arguments"),
                arguments_truncated=bool(d.get("arguments_truncated", False)),
                tool_call_id=d.get("tool_call_id"),
                part_index=d.get("part_index", 0) or 0,
                part_total=d.get("part_total", 1) or 1,
            )
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
