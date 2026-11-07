"""Produce the native Responses-style transcript from confirmed Harness pages."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict

from evalhive_benchmark import ProtocolError

MAX_TRANSCRIPT_BYTES = 8 * 1024**2
MAX_EVENTS = 100000


class Transcript:
    def __init__(self):
        self.items = OrderedDict()
        self.events, self.bytes = 0, 0

    def add(self, event):
        self.events += 1
        if self.events > MAX_EVENTS:
            raise ProtocolError(
                "NATIVE_TRANSCRIPT_LIMIT", "Native transcript exceeds its event budget", "recording"
            )
        if event["event"] != "message.end" or event.get("parent_session_id"):
            return
        data = event["data"]
        if data.get("channel") not in {None, "text"} or data.get("role") not in {
            "assistant",
            "user",
        }:
            return
        identity = event["source"], event.get("session_id"), data["message_id"]
        native_id = (
            "message-"
            + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        )
        item = {
            "id": native_id,
            "role": data["role"],
            "type": "message",
            "status": "completed",
            "content": [
                {
                    "type": "output_text" if data["role"] == "assistant" else "input_text",
                    "text": data["content"],
                }
            ],
        }
        old = self.items.get(identity)
        size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode())
        previous = (
            0
            if old is None
            else len(json.dumps(old, ensure_ascii=False, separators=(",", ":")).encode())
        )
        self.bytes += size - previous
        if self.bytes > MAX_TRANSCRIPT_BYTES:
            raise ProtocolError(
                "NATIVE_TRANSCRIPT_LIMIT", "Native transcript exceeds its byte budget", "recording"
            )
        self.items[identity] = item

    def messages(self):
        return list(self.items.values())


async def collect(context, scope, completion, *, timeout):
    """A terminal session may still have multiple unread pages."""
    session = completion["outcome"]["attempt_id"]
    transcript, cursor, sequence = Transcript(), 0, 0
    while True:
        page = await context.agent(
            scope,
            "events",
            {"cursor": cursor, "limit": 64},
            session_id=session,
            request_id="mcpmark.transcript." + str(sequence),
            timeout=timeout,
        )
        if page["cursor"] != cursor + len(page["events"]):
            raise ProtocolError(
                "NATIVE_TRANSCRIPT_GAP", "Harness transcript cursor skipped evidence", "recording"
            )
        for event in page["events"]:
            transcript.add(event)
        cursor, sequence = page["cursor"], sequence + 1
        if not page["events"]:
            if not page["closed"]:
                raise ProtocolError(
                    "NATIVE_TRANSCRIPT_OPEN", "Harness transcript is not terminal", "recording"
                )
            return {
                "format_version": "mcpmark.messages.v1",
                "interface": "normalized_harness_messages",
                "attempt_id": session,
                "through_seq": cursor - 1,
                "messages": transcript.messages(),
            }
