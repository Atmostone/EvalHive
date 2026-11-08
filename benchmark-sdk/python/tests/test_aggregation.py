"""Archives verify fixed native populations without selecting executable code."""

from copy import deepcopy

import pytest
from evalhive_benchmark import ProtocolError
from evalhive_benchmark.aggregation import public_record, recompute, verify_record
from evalhive_benchmark.peer import identity


def receipt():
    result = {
        "status": "completed",
        "success": False,
        "metrics": [],
        "native": {"private": "retained tracker"},
        "artifacts": [],
        "completeness": {},
    }
    conditions = {
        "benchmark": {"id": "fixture", "version": "1", "worker_image": "sha256:" + "a" * 64},
        "dataset": {"source_digest": "source"},
    }
    inputs = {
        "format_version": "1.0",
        "origin": {"kind": "trial"},
        "conditions": conditions,
        "conditions_digest": identity(conditions),
        "members": [
            {
                "trial_id": "trial",
                "evaluation_id": "evaluation",
                "kind": "initial",
                "result_digest": identity(result),
                "case": {"case_id": "one"},
            }
        ],
        "payload": {"results": [result], "expected": 2},
    }
    aggregate = {
        "metrics": [{"name": "native_value", "value": 0}],
        "expected": 2,
        "observed": 1,
        "native": {"private": "native diagnostic"},
    }
    return {
        **inputs,
        "population_digest": identity(inputs),
        "status": "completed",
        "evaluation_id": "evaluation",
        "request_id": "aggregate.evaluation",
        "result": aggregate,
        "result_digest": identity(aggregate),
    }


def test_verify_and_recompute_use_explicit_adapter_without_changing_the_record():
    record = receipt()
    original = deepcopy(record)
    assert verify_record(record) == record

    def adapter(payload):
        assert payload == record["payload"]
        payload["results"].clear()
        return deepcopy(record["result"])

    assert recompute(record, adapter) == record["result"]
    assert record == original
    public = public_record(record)
    assert not public["complete"] and public["observed"] == 1 and public["expected"] == 2
    assert "private" not in str(public) and "retained tracker" not in str(public)


@pytest.mark.parametrize(
    "field", ["conditions", "payload", "members", "population_digest", "result", "result_digest"]
)
def test_tampered_archive_is_rejected(field):
    record = receipt()
    record[field] = (
        {}
        if isinstance(record[field], dict)
        else []
        if isinstance(record[field], list)
        else "changed"
    )
    with pytest.raises(ProtocolError):
        verify_record(record)


def test_recompute_rejects_different_native_math():
    record = receipt()

    def adapter(payload):
        result = deepcopy(record["result"])
        result["metrics"][0]["value"] = 1
        return result

    with pytest.raises(ProtocolError, match="differs"):
        recompute(record, adapter)


def test_missing_evidence_never_becomes_a_successful_recompute():
    unavailable = {
        "status": "unavailable",
        "evaluation_id": "evaluation",
        "reason": "MISSING_EVIDENCE",
    }
    assert not public_record(unavailable)["complete"]
    with pytest.raises(ProtocolError, match="completed"):
        recompute(unavailable, lambda payload: pytest.fail("No adapter call is authorized"))
