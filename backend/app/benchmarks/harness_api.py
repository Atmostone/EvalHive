"""Use the shared Harness runner inside a benchmark's fenced execution context."""

import uuid

from app.benchmarks.trials import failure
from app.harnesses.attempts import (
    TERMINAL,
    acknowledge_command,
    heartbeat_attempt,
    ingest_events,
    prepare_configuration,
    queue_command,
)


class BenchmarkAttemptAPI:
    """SQL transport for AttemptRunner; benchmark grading owns task completion.

    Runner tokens and public HTTP callbacks are unnecessary for this local host
    transport. Every write instead revalidates the trial controller and binding.
    """

    def __init__(self, calls):
        self.calls = calls

    async def events(self, batch):
        async with self.calls.runner_transaction() as (db, _, attempt, _):
            return await ingest_events(db, attempt.id, batch)

    async def prepare(self, config):
        async with self.calls.runner_transaction() as (db, trial, attempt, now):
            if (
                trial.phase != "driving"
                or trial.cancel_requested_at is not None
                or trial.deadline_at <= now
            ):
                raise failure(
                    "TRIAL_STOPPING",
                    "Harness configuration cannot start in this phase",
                    "execution",
                )
            return await prepare_configuration(db, attempt.id, config)

    async def heartbeat(self):
        async with self.calls.runner_transaction() as (db, trial, attempt, now):
            if attempt.status not in TERMINAL and (
                trial.cancel_requested_at is not None
                or trial.deadline_at <= now
                or trial.phase == "cleaning"
            ):
                await queue_command(
                    db,
                    attempt.id,
                    "abort",
                    {},
                    command_id=uuid.uuid5(attempt.id, "benchmark.abort"),
                )
            return await heartbeat_attempt(db, attempt.id)

    async def acknowledge(self, command_id, response):
        async with self.calls.runner_transaction() as (db, _, attempt, _):
            return await acknowledge_command(db, attempt.id, uuid.UUID(command_id), response)
