"""Classify SDK facts without interpreting native harness names or messages."""

from app.utils.failures import classify_llm_error

_KINDS = {
    "limit": "cap_hit",
    "timeout": "timeout",
    "harness_error": "infra",
    "recording_error": "infra",
    "agent_error": "agent",
}
_RUNNER_FAILURES = frozenset(
    {
        "CONNECTOR_ERROR",
        "CONNECTOR_FAILED",
        "CONNECTOR_EXIT",
        "INVALID_STREAM",
        "INVALID_MESSAGE",
        "UNSUPPORTED_PROTOCOL",
        "SHUTDOWN_FAILED",
        "SPOOL_FULL",
        "TERMINAL_RECORD_LIMIT",
        "RUNNER_INTERRUPTED",
        "RUNNER_STOPPED",
        "MANIFEST_MISMATCH",
        "ARTIFACT_DELIVERY",
    }
)


def classify_result(result, *, status):
    if status in ("lost", "cancelled"):
        # User intervention is not an observed model failure.
        return "infra"
    error = (result or {}).get("error", {})
    facts = error.get("details", {}).get("normalized_failure", {})
    if facts.get("kind") == "model_error":
        return classify_llm_error(facts.get("exception"), facts.get("status_code"))
    if facts.get("kind") in _KINDS:
        return _KINDS[facts["kind"]]
    if error.get("code") == "DEADLINE":
        return "timeout"
    if error.get("code") in _RUNNER_FAILURES:
        return "infra"
    return None
