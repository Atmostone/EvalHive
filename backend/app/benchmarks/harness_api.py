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
from app.models.execution_attempt import ExecutionCommand


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

    async def command(self, command_id, name, payload):
        async with self.calls.runner_transaction() as (db, trial, attempt, now):
            if name != "abort" and (
                trial.phase != "driving"
                or trial.cancel_requested_at is not None
                or trial.deadline_at <= now
            ):
                raise failure(
                    "TRIAL_STOPPING", "Trial cannot accept a new agent command", "execution"
                )
            return await queue_command(db, attempt.id, name, payload, command_id=command_id)

    async def command_result(self, command_id):
        async with self.calls.runner_transaction() as (db, _, attempt, _):
            command = await db.get(ExecutionCommand, command_id)
            if command is None or command.attempt_id != attempt.id:
                raise failure("AGENT_COMMAND_CONFLICT", "Agent command is unavailable")
            return command.result
