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
TRACE_SCHEMA_VERSION = 3

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


def _parse_dt(value) -> datetime | None:
    """An ISO timestamp off an archived chunk, or None. Never raises: a trace is
    still worth reading when one row's clock is unparseable."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


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


def _progress_echo_of(event_type: str | None, data: dict) -> str | None:
    """The tool a progress ping merely announces, or None (SPA-113).

    The agent emits `agent_progress` when it starts a call — `{iteration,
    tool_name, current_step, recent_output}` — and the call itself is recorded in
    full as a log chunk, with arguments and the complete result. `recent_output` is
    a preview of that same result, not extra evidence. Kept as its own step the
    ping becomes a second occurrence of one action, and the judge counts actions:
    on the run that surfaced this, E-07 scored efficiency 4/10 for a file «written
    three separate times» that was written once.

    Returning the tool NAME rather than a boolean is deliberate — whether the ping
    is really an echo cannot be decided from the event alone. If the chunk never
    arrived (a failed POST), the ping is the only surviving trace of the call and
    must stay. The caller, which can see both lists, makes that call.

    A ping carrying something a chunk never holds — an error, a message, the
    model's own words — is not an echo at all and never enters this path."""
    if event_type != "agent_progress" or not isinstance(data, dict):
        return None
    tool = data.get("tool_name")
    if not tool or any(data.get(k) for k in _EVENT_TEXT_KEYS):
        return None
    return str(tool)


def _event_step(ev) -> dict | None:
    event_type = getattr(ev, "event_type", None)
    data = getattr(ev, "data", None) or {}
    if event_type in _REASONING_EVENTS:
        kind = "reasoning"
    elif event_type in _AGENT_EVENTS:
        kind = "agent"
    else:
        return None
    text = _event_text(data)
    return {
        "kind": kind,
        "event_type": event_type,
        "_progress_echo": _progress_echo_of(event_type, data),
        "tool_name": None,
        "arguments": None,
        "arguments_truncated": False,
        "parts_missing": 0,
        "result_missing": False,
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


def join_tool_call_parts(log_chunks) -> list[dict]:
    """Re-join the parts of one tool output into one logical call.

    Public because it is the single definition of «what counts as one tool call»,
    shared with the Data Lake (E-01) so the frozen `tool_call_count` and the judge's
    step list cannot disagree about how many calls a run made. Callers that only
    need call identity may pass rows without `content`.

    An output over the agent's transport cap arrives as several consecutive rows
    sharing a `tool_call_id`. Left alone, each becomes its own step — separately
    truncated, separately counted by the loop detector — so one call reads as three.
    Only *consecutive* rows are merged: a provider that reuses ids across turns must
    not fuse two genuinely different calls. Rows with no call id (pre-SPA-86 rows,
    legacy archives) keep one step per chunk.

    Parts can go missing — the agent suppresses failed POSTs — and a gap must not be
    spliced over silently: joining part 0 to part 2 fabricates a contiguous output
    that never existed. Every gap is marked, and a call whose result never arrived
    (part 0 alone: the tool hung, raised, or the container died) says so."""
    joined: list[dict] = []
    for chunk in log_chunks:
        call_id = _chunk_attr(chunk, "tool_call_id")
        content = _chunk_attr(chunk, "content", "") or ""
        part_index = _chunk_attr(chunk, "part_index", 0)
        part_total = _chunk_attr(chunk, "part_total", 1)
        prev = joined[-1] if joined else None
        if call_id and prev is not None and prev["tool_call_id"] == call_id:
            prev["parts"].append((part_index, content))
            prev["part_total"] = max(prev["part_total"], part_total)
            if prev["arguments"] is None:
                prev["arguments"] = _chunk_attr(chunk, "arguments")
            if not prev.get("reasoning"):
                prev["reasoning"] = _chunk_attr(chunk, "reasoning")
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
                # SPA-114 — the deliberation that preceded this call. Rides on
                # part 0 (the call record), like the arguments do.
                "reasoning": _chunk_attr(chunk, "reasoning"),
                "part_total": part_total,
                "parts": [(part_index, content)],
            }
        )

    for entry in joined:
        parts = sorted(entry.pop("parts"), key=lambda p: p[0])
        seen = {i for i, _ in parts}
        expected = max(entry["part_total"], max(seen) + 1)
        pieces: list[str] = []
        gaps = 0
        for i in range(expected):
            if i in seen:
                pieces.extend(c for j, c in parts if j == i)
            else:
                gaps += 1
                pieces.append(f"\n…[part {i} of this tool output was not recorded]…\n")
        entry["content"] = "".join(pieces)
        entry["parts_missing"] = gaps
        # Part 0 is the call record the agent writes before running the tool, and a
        # result always bumps `part_total` past 1. So «recorded a call, never a
        # result» is exactly: nothing carries content and no row ever announced a
        # second part. Stated this way it needs no format flag — a pre-SPA-86 row,
        # which put the result itself in part 0, has content and is unaffected.
        entry["result_missing"] = (
            bool(entry["tool_call_id"])
            and entry["part_total"] <= 1
            and not any(c.strip() for _, c in parts)
        )
        if entry["result_missing"]:
            entry["content"] = (
                "…[no result recorded for this call — the tool did not return "
                "(hang, crash, or an unreported error)]…"
            )
        del entry["part_total"]
    return joined


def _is_reasoning_carrier(chunk: dict) -> bool:
    """A chunk that exists only to carry reasoning — no tool, no output, no call."""
    return (
        bool((chunk.get("reasoning") or "").strip())
        and not chunk.get("tool_name")
        and not chunk.get("tool_call_id")
        and not (chunk.get("content") or "").strip()
    )


def _reasoning_step(chunk: dict, order: int) -> dict | None:
    """The model's own deliberation as a step of its own (SPA-114).

    Its own kind rather than a field on the tool step, for two reasons: the trim
    policy has to be able to cap and sacrifice it independently — it is verbose
    by construction and would otherwise crowd out the tool calls it is supposed
    to explain — and a reader has to be able to tell «what the model thought»
    from «what it did» at a glance. Ordered just ahead of its call, because that
    is when it happened.
    """
    text = (chunk.get("reasoning") or "").strip()
    if not text:
        return None
    return {
        "kind": "model_reasoning",
        "event_type": None,
        "tool_name": None,
        "arguments": None,
        "arguments_truncated": False,
        "parts_missing": 0,
        "result_missing": False,
        "content": text,
        "_ts": _ts(chunk.get("created_at")),
        "_order": order,
    }


def _chunk_step(chunk: dict, order: int) -> dict:
    return {
        "kind": "tool",
        "event_type": None,
        "tool_name": chunk.get("tool_name"),
        "arguments": chunk.get("arguments"),
        "arguments_truncated": bool(chunk.get("arguments_truncated")),
        "parts_missing": int(chunk.get("parts_missing") or 0),
        "result_missing": bool(chunk.get("result_missing")),
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
        calls = join_tool_call_parts(list(log_chunks or []))

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

        # Each `agent_spawned` starts an attempt. The event itself is dropped from
        # the trace (it is the system snapshot), but its clock is what tells a
        # retry apart from a repetition — without it two attempts read as one run
        # that did everything twice, and repetition is precisely what the process
        # judge is asked to grade (SPA-113).
        spawns = sorted(
            t
            for ev in events
            if getattr(ev, "event_type", None) == "agent_spawned"
            and (t := _ts(getattr(ev, "created_at", None))) is not None
        )
        n_attempts = max(1, len(spawns))

        def _attempt_of(ts: float | None) -> int:
            # An undated step (a pre-SPA-113 archive) can only be placed after
            # everything dated, so the trace can honestly attribute it to the last
            # attempt and nothing finer.
            if ts is None:
                return n_attempts
            return max(1, sum(1 for s_ts in spawns if s_ts <= ts))

        raw_steps = [s for s in (_event_step(ev) for ev in events) if s is not None]
        for i, call in enumerate(calls):
            # A pair of orders per call leaves room for the reasoning step to sit
            # immediately ahead of the call it preceded when both share a stamp.
            if (rs := _reasoning_step(call, order=2 * i)) is not None:
                raw_steps.append(rs)
            # The final turn has no tool call to ride on, so its deliberation
            # arrives on a carrier chunk with no tool, no output and no call id.
            # That carrier is not itself a step — emitting it would put a nameless
            # empty tool call in the trace and count it as an action.
            if _is_reasoning_carrier(call):
                continue
            raw_steps.append(_chunk_step(call, order=2 * i + 1))

        # Chronological merge: dated items ascending, undated (archive) last in
        # their original order (stable sort).
        raw_steps.sort(key=lambda s: (s["_ts"] is None, s["_ts"] or 0.0, s["_order"]))

        for s in raw_steps:
            s["attempt"] = _attempt_of(s["_ts"])

        # An announcement is an echo only when the thing it announced was actually
        # recorded. Resolved here, where both the pings and the tool steps are in
        # hand: a ping whose chunk never arrived is the only surviving trace of
        # that call and stays.
        # Counted, not merely present. Membership was enough only while a tool
        # name appeared once per attempt: with two `search` calls of which one
        # lost its chunk, both pings matched the single recorded call and both
        # were dropped — erasing the call the surviving ping was the last trace
        # of, which is the very case the exception below exists for. A ping is
        # redundant only up to the number of calls actually recorded.
        budget: dict[tuple, int] = {}
        for s in raw_steps:
            if s["kind"] == "tool" and s["tool_name"]:
                key = (s["attempt"], s["tool_name"])
                budget[key] = budget.get(key, 0) + 1
        before = len(raw_steps)
        kept: list[dict] = []
        for s in raw_steps:
            echo = s.get("_progress_echo")
            key = (s["attempt"], echo) if echo else None
            if key is not None and budget.get(key, 0) > 0:
                budget[key] -= 1
                continue
            kept.append(s)
        raw_steps = kept
        progress_echoes_dropped = before - len(raw_steps)

        # The same fact written twice is not two facts. The orchestrator logs one
        # decision as both `orchestrator_reasoning` and `orchestrator_decision`
        # with identical text, and the judge reads the pair as the agent doing
        # something twice — on the live run it cited «template_selected twice in
        # steps 2-3» and docked efficiency for it. Only adjacent, byte-identical,
        # non-tool steps collapse: two identical TOOL calls are two real actions,
        # and the loop counter exists precisely to notice them.
        deduped: list[dict] = []
        for s in raw_steps:
            prev = deduped[-1] if deduped else None
            if (
                prev is not None
                and s["kind"] != "tool"
                and s["kind"] == prev["kind"]
                and s["attempt"] == prev["attempt"]
                and s["content"].strip()
                and s["content"] == prev["content"]
            ):
                continue
            deduped.append(s)
        duplicate_steps_dropped = len(raw_steps) - len(deduped)
        raw_steps = deduped

        # A visible boundary, not just a number on each step: the judge reads prose,
        # and «the agent tried this twice» has to be legible without arithmetic.
        # Only when there was more than one attempt — a marker on every single-run
        # trace would be noise on the overwhelmingly common case.
        if n_attempts > 1:
            marked: list[dict] = []
            current = 0
            for s in raw_steps:
                if s["attempt"] != current and s["attempt"] > 1:
                    marked.append(
                        {
                            "kind": "attempt",
                            "event_type": None,
                            "tool_name": None,
                            "arguments": None,
                            "arguments_truncated": False,
                            "parts_missing": 0,
                            "result_missing": False,
                            "content": (
                                f"── attempt {s['attempt']} of {n_attempts}: the previous "
                                "attempt was retried; what follows is a new run of the "
                                "same task, not a repetition within one ──"
                            ),
                            "_ts": s["_ts"],
                            "_order": -1,
                            "attempt": s["attempt"],
                        }
                    )
                current = s["attempt"]
                marked.append(s)
            raw_steps = marked

        steps: list[dict] = []
        steps_truncated = 0
        steps_args_truncated = 0
        steps_result_missing = 0
        steps_parts_missing = 0
        for seq, s in enumerate(raw_steps):
            content = s["content"]
            original = _count_tokens(content)
            truncated = False
            kept = original

            steps_result_missing += int(bool(s.get("result_missing")))
            steps_parts_missing += int(s.get("parts_missing") or 0)

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
                    "attempt": int(s.get("attempt") or 1),
                    "kind": s["kind"],
                    "tool_name": s["tool_name"],
                    "arguments": arguments,
                    "arguments_truncated": args_truncated,
                    "parts_missing": int(s.get("parts_missing") or 0),
                    "result_missing": bool(s.get("result_missing")),
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
                "steps_result_missing": steps_result_missing,
                "steps_parts_missing": steps_parts_missing,
                "events_dropped": events_dropped,
                # Progress pings removed as echoes of a recorded tool call (SPA-113).
                "progress_echoes_dropped": progress_echoes_dropped,
                # Adjacent non-tool steps that repeated the previous one verbatim.
                "duplicate_steps_dropped": duplicate_steps_dropped,
                # How many times the agent was run for this task. >1 means the
                # trace spans retries and repetition across the boundary is not a
                # loop (SPA-113).
                "attempts": n_attempts,
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
                "steps_result_missing": 0,
                "steps_parts_missing": 0,
                "events_dropped": 0,
                "progress_echoes_dropped": 0,
                "duplicate_steps_dropped": 0,
                "attempts": 1,
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
    preserves the tool call — name, arguments, part identity and, since SPA-113,
    the chunk's timestamp; legacy plain-text archives lose them (degrade
    gracefully). An archive written before SPA-113 has no timestamp, and its tool
    steps sort last, exactly as they did then."""
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
                created_at=_parse_dt(d.get("created_at")),
                # The model's deliberation (SPA-114). Encoder, decoder AND this
                # reconstruction all have to carry it, or a compacted run loses
                # what a live one shows — the same asymmetry SPA-113 fixed for
                # the clock, one layer further in.
                reasoning=d.get("reasoning"),
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
