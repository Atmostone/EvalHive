"""Unit tests for the Redis→WebSocket event relay (no Redis / no DB).

Guards SPA-112: the subscriber used to die on the first idle gap — redis-py 8.x
defaults socket_timeout to 5s, so a blocking listen() raised TimeoutError as soon
as the channel went quiet — and never resubscribed, silently killing every live
stream in the UI for the lifetime of the process.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.utils import events


class FakePubSub:
    """Scripted pubsub: each script entry is a message dict, None (idle), or an Exception."""

    def __init__(self, script: list):
        self.script = list(script)
        self.subscribed_to: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed_to.append(channel)

    async def get_message(self, ignore_subscribe_messages: bool = False, timeout: float = 0.0):
        if not self.script:
            await asyncio.sleep(3600)  # nothing left to say; block until cancelled
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis:
    """Hands out a new FakePubSub per subscribe cycle, from a queue of scripts."""

    def __init__(self, scripts: list[list]):
        self.scripts = list(scripts)
        self.pubsubs: list[FakePubSub] = []
        self.published: list[tuple[str, str]] = []

    def pubsub(self) -> FakePubSub:
        script = self.scripts.pop(0) if self.scripts else []
        sub = FakePubSub(script)
        self.pubsubs.append(sub)
        return sub

    async def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))


def _message(payload: dict) -> dict:
    return {"type": "message", "data": json.dumps(payload)}


@pytest.fixture
def relay(monkeypatch):
    """Capture what the relay fans out locally, and keep backoff from slowing tests."""
    delivered: list[dict] = []

    async def _capture(event_dict: dict) -> None:
        delivered.append(event_dict)

    monkeypatch.setattr(events, "_broadcast_event_local", _capture)
    monkeypatch.setattr(events, "_SUBSCRIBER_POLL_SECONDS", 0.01)
    monkeypatch.setattr(events, "_SUBSCRIBER_BACKOFF_SECONDS", (0.01,))
    monkeypatch.setattr(events, "_subscriber_live", False)
    yield delivered
    monkeypatch.setattr(events, "_subscriber_live", False)


async def _run_consumer(delivered: list[dict], expected: int, timeout: float = 2.0) -> asyncio.Task:
    """Start _consume() and wait until it has delivered `expected` events."""
    task = asyncio.create_task(events._consume())
    deadline = asyncio.get_event_loop().time() + timeout
    while len(delivered) < expected and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.01)
    return task


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_idle_polls_do_not_kill_the_subscriber(monkeypatch, relay):
    """An idle channel yields None repeatedly; the relay keeps consuming (the SPA-112 regression)."""
    fake = FakeRedis([[None, None, None, _message({"event_type": "agent_progress"})]])
    monkeypatch.setattr(events, "_redis_publisher", fake)

    task = await _run_consumer(relay, expected=1)
    try:
        assert relay == [{"event_type": "agent_progress"}]
        assert len(fake.pubsubs) == 1, "an idle gap must not force a resubscribe"
        assert events._subscriber_live is True
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_dropped_connection_resubscribes_and_keeps_delivering(monkeypatch, relay):
    """A read failure is survivable: the relay resubscribes and later events still arrive."""
    fake = FakeRedis(
        [
            [TimeoutError("Timeout reading from redis:6379")],
            [_message({"event_type": "after_reconnect"})],
        ]
    )
    monkeypatch.setattr(events, "_redis_publisher", fake)

    task = await _run_consumer(relay, expected=1)
    try:
        assert relay == [{"event_type": "after_reconnect"}]
        assert len(fake.pubsubs) == 2
        assert fake.pubsubs[0].closed is True, "the dead pubsub must be released"
        assert [s.subscribed_to for s in fake.pubsubs] == [
            [events.EVENTS_CHANNEL],
            [events.EVENTS_CHANNEL],
        ]
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_undecodable_payload_is_skipped_not_fatal(monkeypatch, relay):
    """Garbage on the channel is dropped without taking the subscription down with it."""
    fake = FakeRedis(
        [[{"type": "message", "data": "not-json"}, _message({"event_type": "still_here"})]]
    )
    monkeypatch.setattr(events, "_redis_publisher", fake)

    task = await _run_consumer(relay, expected=1)
    try:
        assert relay == [{"event_type": "still_here"}]
        assert len(fake.pubsubs) == 1
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_broadcast_falls_back_to_local_while_subscriber_is_down(monkeypatch, relay):
    """With no live subscriber, publishing is not a delivery path — broadcast locally too."""
    fake = FakeRedis([])
    monkeypatch.setattr(events, "_redis_publisher", fake)
    monkeypatch.setattr(events, "_subscriber_live", False)

    await events._broadcast_event({"event_type": "orphaned"})

    assert relay == [{"event_type": "orphaned"}], "client would otherwise see nothing at all"
    assert len(fake.published) == 1, "still published, in case another replica is listening"


@pytest.mark.asyncio
async def test_broadcast_defers_to_the_subscriber_when_it_is_live(monkeypatch, relay):
    """A live subscriber delivers the round-trip copy; broadcasting locally would duplicate it."""
    fake = FakeRedis([])
    monkeypatch.setattr(events, "_redis_publisher", fake)
    monkeypatch.setattr(events, "_subscriber_live", True)

    await events._broadcast_event({"event_type": "round_trip"})

    assert relay == []
    assert len(fake.published) == 1
