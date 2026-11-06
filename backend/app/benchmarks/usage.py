"""Rebuildable role/model usage ledger over immutable native usage observations."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, localcontext

from evalhive_benchmark import validate

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
AXES = ("role", "model", "purpose", "mode")


def _amount(values):
    # SDK amounts are decimal strings. Preserve exact decimal arithmetic instead
    # of binary floats, including estimates smaller than one cent.
    with localcontext() as context:
        context.prec = 256
        return format(sum((Decimal(value) for value in values), Decimal(0)), "f")


class UsageLedger:
    """usage_id identifies one model request within a registered actor stream.

    Delta records are a single immutable measurement, not a new charge each time
    they are announced. Cumulative records replace that request's prior snapshot;
    they never add a final total to intermediate totals. Mixed modes, dimensions
    or decreasing known token counters invalidate that call, while preserving
    all raw journal observations for diagnosis. Adapters must use different IDs
    for different requests and must not publish overlapping summary series.
    """

    def __init__(self):
        self.calls, self.errors = {}, []

    def add(self, stream, sequence, usage):
        validate("usage", usage)
        key = str(stream), usage["usage_id"]
        previous = self.calls.get(key)
        code = None
        if previous:
            old = previous["usage"]
            if sequence <= previous["sequence"]:
                code = "USAGE_ORDER_CONFLICT"
            elif any(old.get(axis) != usage.get(axis) for axis in AXES):
                code = "USAGE_DIMENSION_CONFLICT"
            elif usage["mode"] == "delta" and old != usage:
                code = "USAGE_DELTA_CONFLICT"
            elif usage["mode"] == "cumulative" and any(
                old.get(field) is not None
                and usage.get(field) is not None
                and usage[field] < old[field]
                for field in TOKEN_FIELDS
            ):
                code = "USAGE_COUNTER_DECREASE"
        cost = usage.get("cost")
        if cost is not None and len(cost["amount"]) > 80:
            code = "USAGE_COST_OUT_OF_RANGE"
        invalid = bool(code) or bool(previous and previous["invalid"])
        if code:
            self.errors.append(
                {
                    "identity": hashlib.sha256((key[0] + ":" + key[1]).encode()).hexdigest(),
                    "code": code,
                }
            )
        self.calls[key] = {
            "usage": deepcopy(previous["usage"] if previous and invalid else usage),
            "sequence": sequence,
            "invalid": invalid,
        }

    def summary(self):
        groups = defaultdict(list)
        for call in self.calls.values():
            usage = call["usage"]
            groups[tuple(usage.get(axis) for axis in AXES[:3])].append(call)
        output = []
        for (role, model, purpose), calls in sorted(
            groups.items(), key=lambda item: tuple(value or "" for value in item[0])
        ):
            tokens = {}
            for field in TOKEN_FIELDS:
                known = [
                    call["usage"][field]
                    for call in calls
                    if not call["invalid"] and call["usage"].get(field) is not None
                ]
                missing = len(calls) - len(known)
                tokens[field] = {
                    "value": None if missing else sum(known),
                    "known_sum": sum(known),
                    "unknown_calls": missing,
                }
            costs = [
                call["usage"]["cost"]
                for call in calls
                if not call["invalid"] and call["usage"].get("cost") is not None
            ]
            missing = len(calls) - len(costs)
            output.append(
                {
                    "role": role,
                    "model": model,
                    "purpose": purpose,
                    "calls": len(calls),
                    "tokens": tokens,
                    "cost": {
                        "currency": "USD",
                        "amount": None if missing else _amount([cost["amount"] for cost in costs]),
                        "known_amount": _amount([cost["amount"] for cost in costs]),
                        "unknown_calls": missing,
                        "sources": sorted({cost["source"] for cost in costs}),
                    },
                }
            )
        return {
            "groups": output,
            "valid": not self.errors,
            "errors": deepcopy(self.errors),
            "measurement_complete": bool(output)
            and not self.errors
            and all(
                all(value["unknown_calls"] == 0 for value in group["tokens"].values())
                and group["cost"]["unknown_calls"] == 0
                for group in output
            ),
        }


async def trial_usage(store, workspace_id, trial_id):
    """Read execution usage without including independent evaluator invocations."""
    from app.models.benchmark_trial import BenchmarkJournalEntry, BenchmarkTrial

    return await _journal_usage(
        store,
        BenchmarkTrial,
        BenchmarkJournalEntry,
        trial_id,
        (BenchmarkTrial.workspace_id == workspace_id,),
        "trial_id",
        "TRIAL_NOT_FOUND",
    )


async def evaluation_usage(store, workspace_id, trial_id, evaluation_id):
    """Read only one authorized regrade invocation, with its own journal watermark."""
    from app.models.benchmark_evaluation_control import (
        BenchmarkEvaluationControl,
        BenchmarkEvaluationJournal,
    )

    return await _journal_usage(
        store,
        BenchmarkEvaluationControl,
        BenchmarkEvaluationJournal,
        evaluation_id,
        (
            BenchmarkEvaluationControl.workspace_id == workspace_id,
            BenchmarkEvaluationControl.source_trial_id == trial_id,
        ),
        "control_id",
        "EVALUATION_NOT_FOUND",
    )


async def _journal_usage(store, control, journal, identifier, scope, journal_key, missing_code):
    """Read a scoped journal in bounded pages; raw payloads stay private."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import undefer

    from app.benchmarks.trials import failure

    ledger, after = UsageLedger(), -1
    async with AsyncSession(store.engine) as db:
        high = await db.scalar(select(control.next_seq).where(control.id == identifier, *scope))
        if high is None:
            raise failure(missing_code, "Usage journal is unavailable in this workspace")
        # A stable high-water mark keeps a live stream from extending this read.
        while True:
            rows = (
                await db.scalars(
                    select(journal)
                    .where(
                        getattr(journal, journal_key) == identifier,
                        journal.event == "usage",
                        journal.seq > after,
                        journal.seq < high,
                    )
                    .options(undefer(journal.private_data))
                    .order_by(journal.seq)
                    .limit(128)
                )
            ).all()
            if not rows:
                break
            for row in rows:
                event = store.private.open("journal:" + str(row.id), row.private_data)
                ledger.add(row.stream_id, row.seq, event["data"])
            after = rows[-1].seq
    return {**ledger.summary(), "through_seq": high - 1}
