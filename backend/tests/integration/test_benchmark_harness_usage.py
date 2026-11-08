"""Harness usage shares the trial ledger atomically without replay charges."""

# ruff: noqa: F811

import asyncio
import uuid
from copy import deepcopy

import pytest
from app.benchmarks.harness_api import BenchmarkAttemptAPI
from app.benchmarks.usage import trial_usage
from app.models.benchmark_trial import BenchmarkJournalEntry
from evalhive_benchmark import ProtocolError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.test_benchmark_agent_calls import calls, fixture, start  # noqa: F401


def event(caller, sequence, name, data, *, session="main-session"):
    return {
        "protocol_version": "1.0",
        "type": "event",
        "event_id": str(uuid.uuid4()),
        "attempt_id": str(caller.session_id),
        "seq": sequence,
        "source": "fixture.harness",
        "session_id": session,
        "event": name,
        "data": data,
    }


async def test_harness_usage_is_committed_with_events_and_repeated_delivery_does_not_charge(calls):
    await start(calls)
    api = BenchmarkAttemptAPI(calls)
    initial = {
        "usage_id": "same-native-id",
        "mode": "cumulative",
        "purpose": "agent",
        "model": "fixture-model",
        "input_tokens": None,
        "output_tokens": None,
        "cost": None,
    }
    observed = {
        **initial,
        "input_tokens": 13,
        "output_tokens": 4,
        "cost": {"amount": "000.020", "currency": "USD", "source": "configured"},
    }
    nested = {**observed, "input_tokens": 2, "output_tokens": 1, "cost": None}
    batch = [
        event(calls, 0, "run.started", {}),
        event(calls, 1, "usage", initial),
        event(calls, 2, "usage", observed),
        event(calls, 3, "usage", nested, session="nested-session"),
    ]
    results = await asyncio.gather(api.events(batch), api.events(deepcopy(batch)))
    assert sum(result["accepted"] for result in results) == 4
    assert sum(result["duplicates"] for result in results) == 4
    usage = await trial_usage(calls.trials, calls.owner.workspace_id, calls.owner.trial_id)
    (group,) = usage["groups"]
    assert group["role"] == "agent" and group["calls"] == 2
    assert group["tokens"]["input_tokens"]["value"] == 15
    assert group["tokens"]["output_tokens"]["value"] == 5
    assert group["cost"]["known_amount"] == "0.020" and group["cost"]["amount"] is None
    assert group["cost"]["sources"] == ["estimate"] and usage["valid"]
    async with AsyncSession(calls.trials.engine) as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(BenchmarkJournalEntry)
                .where(
                    BenchmarkJournalEntry.trial_id == calls.owner.trial_id,
                    BenchmarkJournalEntry.event == "usage",
                )
            )
            == 3
        )
    with pytest.raises(ProtocolError):
        await trial_usage(calls.trials, uuid.uuid4(), calls.owner.trial_id)
