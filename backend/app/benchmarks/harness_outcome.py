"""Typed agent outcomes come from committed Harness SDK attempts, not drive RPCs."""

from dataclasses import dataclass
from uuid import UUID

from evalhive_benchmark import ProtocolError
from evalhive_benchmark.peer import identity
from evalhive_harness import ProtocolError as HarnessProtocolError
from evalhive_harness import validate


@dataclass(frozen=True)
class HarnessExecutionBinding:
    workspace_id: UUID
    task_id: UUID
    trial_id: UUID
    generation: int
    controller_id: UUID

    def metadata(self):
        return {
            "format_version": "1.0",
            "trial_id": str(self.trial_id),
            "generation": self.generation,
            "controller_id": str(self.controller_id),
        }


@dataclass(frozen=True)
class HarnessExecutionOutcome:
    attempt_id: UUID
    status: str
    recording_status: str
    evidence_digest: str

    def reference(self):
        return {"attempt_id": str(self.attempt_id), "evidence_digest": self.evidence_digest}

    def confirm_reference(self, reference):
        if reference != self.reference():
            raise ProtocolError(
                "HARNESS_OUTCOME_CONFLICT",
                "Drive does not reference the confirmed harness outcome",
                "recording",
            )

    @classmethod
    def from_attempt(cls, attempt, binding):
        if (attempt.workspace_id, attempt.task_id) != (
            binding.workspace_id,
            binding.task_id,
        ) or attempt.run_spec.get("metadata", {}).get("benchmark_binding") != binding.metadata():
            raise ProtocolError(
                "FOREIGN_HARNESS_ATTEMPT",
                "Harness attempt belongs to another execution context",
                "configuration",
            )
        if (
            attempt.status not in {"success", "failed", "cancelled", "lost"}
            or attempt.finished_at is None
        ):
            raise ProtocolError(
                "HARNESS_RUNNING", "A committed terminal harness outcome is required", "execution"
            )
        try:
            validate("result", attempt.result)
        except HarnessProtocolError:
            raise ProtocolError(
                "HARNESS_RESULT_INVALID", "Committed harness result is invalid", "recording"
            ) from None
        expected = "failed" if attempt.status == "lost" else attempt.status
        if attempt.result["status"] != expected:
            raise ProtocolError(
                "HARNESS_RESULT_CONFLICT", "Harness status and terminal result differ", "recording"
            )
        delivery = attempt.completeness.get("event_delivery")
        if attempt.status == "success" and delivery != "contiguous":
            raise ProtocolError(
                "HARNESS_RECORDING_INCOMPLETE",
                "Successful execution requires contiguous final delivery",
                "recording",
            )
        recorded = "complete" if delivery == "contiguous" else "interrupted"
        digest = identity(
            {
                "attempt_id": str(attempt.id),
                "workspace_id": str(attempt.workspace_id),
                "task_id": str(attempt.task_id),
                "binding": binding.metadata(),
                "status": attempt.status,
                "result": attempt.result,
                "event_delivery": delivery,
            }
        )
        return cls(attempt.id, attempt.status, recorded, digest)
