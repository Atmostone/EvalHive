"""A completed connector call cannot replace authoritative agent execution evidence."""

import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.benchmarks.harness_outcome import HarnessExecutionBinding, HarnessExecutionOutcome
from evalhive_benchmark import ProtocolError


def committed(status="success"):
    binding = HarnessExecutionBinding(*(uuid.uuid4() for _ in range(3)), 1, uuid.uuid4())
    result = {"status": "failed" if status == "lost" else status, "summary": "Fixture completion"}
    if result["status"] == "failed":
        result["error"] = {"code": "FIXTURE_FAILED", "message": "Fixture failure"}
    attempt = SimpleNamespace(
        id=uuid.uuid4(),
        workspace_id=binding.workspace_id,
        task_id=binding.task_id,
        run_spec={"metadata": {"benchmark_binding": binding.metadata()}},
        status=status,
        finished_at=datetime.now(UTC),
        result=result,
        completeness={"event_delivery": "interrupted" if status == "lost" else "contiguous"},
    )
    return attempt, binding


@pytest.mark.parametrize("status", ["success", "failed", "cancelled", "lost"])
def test_harness_failure_cancellation_and_loss_are_not_promoted_to_success(status):
    attempt, binding = committed(status)
    outcome = HarnessExecutionOutcome.from_attempt(attempt, binding)
    assert outcome.status == status
    assert outcome.recording_status == ("interrupted" if status == "lost" else "complete")
    outcome.confirm_reference(outcome.reference())
    with pytest.raises(ProtocolError):
        outcome.confirm_reference({"finished": True})


@pytest.mark.parametrize(
    "field", ["workspace_id", "task_id", "trial_id", "controller_id", "generation"]
)
def test_outcome_cannot_cross_a_task_trial_or_controller_epoch(field):
    attempt, binding = committed()
    wrong = replace(binding, **{field: 2 if field == "generation" else uuid.uuid4()})
    with pytest.raises(ProtocolError) as error:
        HarnessExecutionOutcome.from_attempt(attempt, wrong)
    assert error.value.code == "FOREIGN_HARNESS_ATTEMPT"


@pytest.mark.parametrize(
    "problem,code",
    [
        ("running", "HARNESS_RUNNING"),
        ("no_time", "HARNESS_RUNNING"),
        ("rpc_result", "HARNESS_RESULT_INVALID"),
        ("contradictory", "HARNESS_RESULT_CONFLICT"),
        ("incomplete", "HARNESS_RECORDING_INCOMPLETE"),
    ],
)
def test_terminal_event_evidence_is_required(problem, code):
    attempt, binding = committed()
    if problem == "running":
        attempt.status = "running"
    if problem == "no_time":
        attempt.finished_at = None
    if problem == "rpc_result":
        attempt.result = {"finished": True}
    if problem == "contradictory":
        attempt.result = {"status": "cancelled", "summary": "Cancelled"}
    if problem == "incomplete":
        attempt.completeness["event_delivery"] = "interrupted"
    with pytest.raises(ProtocolError) as error:
        HarnessExecutionOutcome.from_attempt(attempt, binding)
    assert error.value.code == code


def test_reference_binds_final_result_but_not_later_ui_projection_fields():
    attempt, binding = committed()
    original = HarnessExecutionOutcome.from_attempt(attempt, binding)
    attempt.completeness["task_projection"] = "committed"
    assert (
        HarnessExecutionOutcome.from_attempt(attempt, binding).reference() == original.reference()
    )
    changed = deepcopy(attempt)
    changed.result["summary"] = "Different terminal evidence"
    other = HarnessExecutionOutcome.from_attempt(changed, binding)
    with pytest.raises(ProtocolError) as error:
        original.confirm_reference(other.reference())
    assert error.value.code == "HARNESS_OUTCOME_CONFLICT"
