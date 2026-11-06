"""Failure classification consumes the SDK contract, never harness-specific text."""

import pytest
from app.harnesses.failures import classify_result


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        ({"kind": "model_error", "status_code": 401}, "llm_auth"),
        ({"kind": "model_error", "status_code": 429}, "llm_rate_limit"),
        ({"kind": "model_error", "status_code": 503}, "llm_transient"),
        ({"kind": "model_error", "exception": "APIConnectionError"}, "llm_transient"),
        ({"kind": "model_error", "status_code": 400}, "llm_error"),
        ({"kind": "limit"}, "cap_hit"),
        ({"kind": "timeout"}, "timeout"),
        ({"kind": "recording_error"}, "infra"),
        ({"kind": "harness_error"}, "infra"),
        ({"kind": "agent_error"}, "agent"),
    ],
)
def test_connector_neutral_failure_facts(facts, expected):
    result = {"error": {"code": "NATIVE_ARBITRARY", "details": {"normalized_failure": facts}}}
    assert classify_result(result, status="failed") == expected


def test_runtime_loss_and_user_intervention_do_not_measure_model_failure():
    assert classify_result(None, status="lost") == "infra"
    assert classify_result(None, status="cancelled") == "infra"
    assert classify_result({"error": {"code": "DEADLINE"}}, status="failed") == "timeout"
    assert classify_result({"error": {"code": "SPOOL_FULL"}}, status="failed") == "infra"
    assert classify_result({"error": {"code": "TIMEOUT"}}, status="failed") == "infra"
    assert classify_result({"error": {"code": "ARBITRARY_VALIDATION"},
                            "metadata": {"harness_started": False}}, status="failed") == "infra"
    assert classify_result({"error": {"code": "UNCLASSIFIED_NATIVE"}}, status="failed") is None
