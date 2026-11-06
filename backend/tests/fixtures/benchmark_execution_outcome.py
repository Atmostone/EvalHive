"""Explicit trusted stand-in for lifecycle fixtures that do not run an agent."""

import uuid

from app.benchmarks.harness_outcome import HarnessExecutionOutcome
from evalhive_benchmark import ProtocolError
from evalhive_benchmark.peer import identity


def fixture_execution_outcome(owner, *, status="success", recording_status="complete"):
    async def resolve(reference):
        if reference is not None:
            raise ProtocolError(
                "UNEXPECTED_FIXTURE_REFERENCE",
                "Lifecycle fixture has no agent session",
                "recording",
            )
        return HarnessExecutionOutcome(
            uuid.uuid5(owner.controller_id, "fixture.harness"),
            status,
            recording_status,
            identity(
                {
                    "trial_id": str(owner.trial_id),
                    "controller_id": str(owner.controller_id),
                    "status": status,
                }
            ),
        )

    return resolve
