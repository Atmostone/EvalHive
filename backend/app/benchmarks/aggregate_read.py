"""Authenticate aggregation journals and expose only their public projection."""

import uuid

from evalhive_benchmark import ProtocolError
from evalhive_benchmark.aggregation import public_record, verify_record
from evalhive_benchmark.peer import identity
from sqlalchemy import literal, select, union_all
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import undefer

from app.models.benchmark_command import BenchmarkCommand
from app.models.benchmark_evaluation_control import (
    BenchmarkEvaluationControl,
    BenchmarkEvaluationJournal,
)
from app.models.benchmark_trial import BenchmarkEventStream, BenchmarkJournalEntry, BenchmarkTrial

EVENTS = ("aggregate.completed", "aggregate.unavailable")


async def read_records(db, store, workspace_id, trial_id, *, offset=0, limit=20):
    first = (
        select(
            BenchmarkJournalEntry.id,
            BenchmarkJournalEntry.private_data,
            BenchmarkJournalEntry.created_at,
            BenchmarkJournalEntry.generation,
            literal(None, type_=UUID(as_uuid=True)).label("control_id"),
        )
        .join(BenchmarkTrial, BenchmarkTrial.id == BenchmarkJournalEntry.trial_id)
        .where(
            BenchmarkTrial.id == trial_id,
            BenchmarkTrial.workspace_id == workspace_id,
            BenchmarkJournalEntry.event.in_(EVENTS),
        )
    )
    second = (
        select(
            BenchmarkEvaluationJournal.id,
            BenchmarkEvaluationJournal.private_data,
            BenchmarkEvaluationJournal.created_at,
            BenchmarkEvaluationJournal.generation,
            BenchmarkEvaluationJournal.control_id,
        )
        .join(
            BenchmarkEvaluationControl,
            BenchmarkEvaluationControl.id == BenchmarkEvaluationJournal.control_id,
        )
        .where(
            BenchmarkEvaluationControl.source_trial_id == trial_id,
            BenchmarkEvaluationControl.workspace_id == workspace_id,
            BenchmarkEvaluationJournal.event.in_(EVENTS),
        )
    )
    combined = union_all(first, second).subquery()
    rows = list(
        await db.execute(
            select(combined)
            .order_by(combined.c.created_at, combined.c.id)
            .offset(offset)
            .limit(limit)
        )
    )
    output = []
    for row in rows:
        try:
            record = verify_record(store.private.open("journal:" + str(row.id), row.private_data))
            if record["status"] == "completed":
                command = await db.scalar(
                    select(BenchmarkCommand)
                    .join(
                        BenchmarkEventStream, BenchmarkEventStream.id == BenchmarkCommand.stream_id
                    )
                    .where(
                        BenchmarkCommand.trial_id == trial_id,
                        BenchmarkCommand.request_id == record["request_id"],
                        BenchmarkCommand.method == "aggregate",
                        BenchmarkCommand.state == "completed",
                        BenchmarkCommand.error_code.is_(None),
                        BenchmarkEventStream.control_id == row.control_id,
                        BenchmarkEventStream.generation == row.generation,
                        BenchmarkEventStream.actor_id == uuid.UUID(record["worker_actor_id"]),
                    )
                    .options(undefer(BenchmarkCommand.private_response))
                )
                if command is None:
                    raise ValueError
                response = store.private.open(
                    "command-response:" + str(command.id), command.private_response
                )
                if (
                    not response.get("ok")
                    or identity(response["result"]) != record["result_digest"]
                ):
                    raise ValueError
            output.append((row.id, row.created_at, record))
        except (ValueError, KeyError, TypeError):
            raise ProtocolError(
                "AGGREGATE_RECEIPT_UNAVAILABLE",
                "Native aggregation receipt cannot be authenticated",
                "recording",
            ) from None
    return output


def project(identifier, created_at, record):
    return {"id": str(identifier), "created_at": created_at, **public_record(record)}
