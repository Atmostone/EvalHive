"""Deterministic compatibility views over the immutable attempt journal."""

from __future__ import annotations

import json
from decimal import Decimal

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def _text(value):
    if value is None:
        return ""
    return (
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    )


def _known_sum(values):
    values = list(values)
    return sum(values) if values and all(value is not None for value in values) else None


def summarize_usage(events):
    """Cumulative samples replace a usage identity; deltas add within it.

    Totals remain unknown when any contributing call omits that measurement.
    Native terminal usage is not a second normalized usage source.
    """
    entries = {}
    for event in events:
        if event["event"] != "usage":
            continue
        data = event["data"]
        key = (event.get("session_id"), data["usage_id"])
        previous = entries.get(key)
        if previous is not None and previous["mode"] != data["mode"]:
            raise ValueError("Usage mode changed within one identity")
        if previous is not None and (
            previous["purpose"] != data["purpose"]
            or (previous.get("model") and data.get("model") and previous["model"] != data["model"])
        ):
            raise ValueError("Usage identity changed its model or purpose")
        current = {**data, "session_id": event.get("session_id")}
        if previous is not None and current.get("model") is None:
            current["model"] = previous.get("model")
        if previous is not None and data["mode"] == "delta":
            current.update(
                {
                    field: _known_sum((previous.get(field), data.get(field)))
                    for field in TOKEN_FIELDS
                }
            )
            first_cost, next_cost = previous.get("cost"), data.get("cost")
            current["cost"] = None
            if (
                first_cost is not None
                and next_cost is not None
                and first_cost["currency"] == next_cost["currency"]
            ):
                current["cost"] = {
                    "amount": str(Decimal(first_cost["amount"]) + Decimal(next_cost["amount"])),
                    "currency": first_cost["currency"],
                    "source": first_cost["source"]
                    if first_cost["source"] == next_cost["source"]
                    else "estimate",
                }
        entries[key] = current
    groups = {}
    for data in entries.values():
        groups.setdefault((data.get("model"), data["purpose"]), []).append(data)

    def total(rows):
        cost = _known_sum(
            Decimal(row["cost"]["amount"])
            if row.get("cost") and row["cost"]["currency"] == "USD"
            else None
            for row in rows
        )
        return {
            **{field: _known_sum(row.get(field) for row in rows) for field in TOKEN_FIELDS},
            "cost_usd": str(cost) if cost is not None else None,
            "cost_sources": sorted({row["cost"]["source"] for row in rows if row.get("cost")}),
            "calls_reported": len(rows),
        }

    return {
        **total(list(entries.values())),
        "reported": bool(entries),
        "by_model_and_purpose": [
            {"model": model, "purpose": purpose, **total(rows)}
            for (model, purpose), rows in groups.items()
        ],
    }


def project_log(events, *, tool_aliases=None):
    """One evolving row per message/call, correlated across interleaved sessions.

    Unknown starts/results stay explicit. Full native records remain in the
    journal; this view is for existing log and trajectory consumers.
    """
    tool_aliases = tool_aliases or {}
    tools, messages, rows = {}, {}, []

    def new_row(event, identity):
        row = {
            "id": identity,
            "chunk_seq": event["seq"],
            "content": "",
            "attempt_id": event["attempt_id"],
            "session_id": event.get("session_id"),
            "parent_session_id": event.get("parent_session_id"),
            "created_at": event.get("observed_at"),
            "tool_name": None,
            "arguments": None,
            "arguments_truncated": False,
            "tool_call_id": None,
            "part_index": 0,
            "part_total": 1,
            "reasoning": None,
        }
        rows.append(row)
        return row

    for event in events:
        kind, data = event["event"], event["data"]
        session = event.get("session_id")
        if kind.startswith("tool."):
            key = (session, data["call_id"])
            if key not in tools:
                identity = json.dumps(
                    [event["attempt_id"], session, data["call_id"]], separators=(",", ":")
                )
                tools[key] = new_row(event, identity)
                tools[key].update(
                    {"tool_call_id": data["call_id"], "start_missing": True, "result_missing": True}
                )
            row = tools[key]
            row["updated_seq"] = event["seq"]
            row["parent_call_id"] = data.get("parent_call_id", row.get("parent_call_id"))
            if kind == "tool.start":
                row.update(
                    {
                        "tool_name": tool_aliases.get(data["name"], data["name"]),
                        "native_tool_name": data["name"],
                        "arguments": data["arguments"],
                        "start_missing": False,
                    }
                )
            elif kind == "tool.update":
                content = _text(data["content"])
                row["content"] = row["content"] + content if data["mode"] == "delta" else content
            elif kind == "tool.end":
                row.update(
                    {
                        "content": _text(data["result"]),
                        "result_missing": False,
                        "tool_status": data["status"],
                        "duration_ms": data.get("duration_ms"),
                    }
                )
        elif kind.startswith("message."):
            key = (session, data["message_id"], data.get("channel", "text"))
            if key not in messages:
                identity = json.dumps([event["attempt_id"], *key], separators=(",", ":"))
                messages[key] = new_row(event, identity)
                messages[key].update({"message_id": data["message_id"], "message_complete": False})
            row = messages[key]
            row["updated_seq"] = event["seq"]
            row["role"] = data.get("role", row.get("role"))
            row["parent_call_id"] = data.get("parent_call_id", row.get("parent_call_id"))
            if "delta" in data:
                row["content"] += data["delta"]
            if "content" in data:
                row["content"] = _text(data["content"])
            if kind == "message.end":
                row["message_complete"] = True
            if data.get("channel") == "reasoning":
                row["reasoning"] = row["content"]
    for row in rows:
        if row.get("message_id") and row.get("reasoning") is not None:
            row["content"] = ""
    return rows


async def load_attempt_view(db, attempt_id):
    """Native payloads stay in storage; compatibility views need normalized data."""
    from sqlalchemy import select

    from app.models.execution_attempt import ExecutionAttempt, ExecutionEvent

    attempt = await db.get(ExecutionAttempt, attempt_id)
    events = list(
        await db.scalars(
            select(ExecutionEvent.payload)
            .where(
                ExecutionEvent.attempt_id == attempt_id,
                ExecutionEvent.event.notin_(["native", "heartbeat"]),
            )
            .order_by(ExecutionEvent.seq)
        )
    )
    chunks = project_log(
        events, tool_aliases=attempt.run_spec.get("metadata", {}).get("tool_aliases")
    )
    calls = [row for row in chunks if row["tool_call_id"] is not None]
    completeness = {
        **attempt.completeness,
        "observed_tools": "incomplete"
        if any(row["result_missing"] or row["start_missing"] for row in calls)
        else "complete"
        if calls
        else "none_observed",
        "reasoning": "observed" if any(row["reasoning"] for row in chunks) else "unknown",
    }
    try:
        usage = summarize_usage(events)
    except ValueError:
        usage = {**summarize_usage([]), "recording_error": "USAGE_IDENTITY_CONFLICT"}
        completeness["usage"] = "invalid"
    return {
        "attempt": attempt,
        "events": events,
        "chunks": chunks,
        "usage": usage,
        "completeness": completeness,
    }


async def publish_log_updates(db, attempt_id, incoming):
    from app.utils.events import broadcast_log_chunk

    calls = {
        (event.get("session_id"), event["data"]["call_id"])
        for event in incoming
        if event["event"].startswith("tool.")
    }
    messages = {
        (event.get("session_id"), event["data"]["message_id"])
        for event in incoming
        if event["event"].startswith("message.")
    }
    if not calls and not messages:
        return
    view = await load_attempt_view(db, attempt_id)
    attempt = view["attempt"]
    for row in view["chunks"]:
        if (row["session_id"], row["tool_call_id"]) in calls or (
            row["session_id"],
            row.get("message_id"),
        ) in messages:
            await broadcast_log_chunk(
                {
                    **row,
                    "task_id": str(attempt.task_id),
                    "workspace_id": str(attempt.workspace_id),
                    "attempt_number": attempt.number,
                }
            )
