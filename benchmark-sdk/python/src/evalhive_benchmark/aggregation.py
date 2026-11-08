"""Portable native-aggregation receipts; verifier code is supplied by the caller."""

from copy import deepcopy

from .peer import identity
from .protocol import ProtocolError, validate

INPUT_FIELDS = ("format_version", "origin", "conditions", "conditions_digest", "members", "payload")


def verify_record(record):
    try:
        if record["status"] not in {"completed", "unavailable"}:
            raise ValueError
        if "population_digest" not in record:
            if record["status"] == "unavailable" and record.get("reason"):
                return record
            raise ValueError
        inputs = {key: record[key] for key in INPUT_FIELDS}
        if inputs["format_version"] != "1.0" or identity(inputs) != record["population_digest"]:
            raise ValueError
        if identity(inputs["conditions"]) != inputs["conditions_digest"]:
            raise ValueError
        members, payload = inputs["members"], inputs["payload"]
        results = payload["results"]
        if (
            not isinstance(members, list)
            or not isinstance(results, list)
            or len(members) != len(results)
            or type(payload["expected"]) is not int
            or payload["expected"] < len(results)
        ):
            raise ValueError
        cases, evaluations, trials = set(), set(), set()
        for member, result in zip(members, results, strict=True):
            validate("evaluation_result", result)
            case = identity(member["case"])
            if (
                case in cases
                or member["evaluation_id"] in evaluations
                or member["trial_id"] in trials
            ):
                raise ValueError
            cases.add(case)
            evaluations.add(member["evaluation_id"])
            trials.add(member["trial_id"])
            if identity(result) != member["result_digest"]:
                raise ValueError
        if record["evaluation_id"] not in evaluations:
            raise ValueError
        if record["status"] == "completed":
            result = validate("aggregate_result", record["result"])
            if (
                identity(result) != record["result_digest"]
                or result["observed"] != len(members)
                or result["expected"] < result["observed"]
                or result["expected"] < payload["expected"]
                or record["request_id"] != "aggregate." + record["evaluation_id"]
            ):
                raise ValueError
        elif not record.get("reason"):
            raise ValueError
        return record
    except (KeyError, TypeError, ValueError):
        raise ProtocolError(
            "AGGREGATE_RECORD_INVALID", "Native aggregate receipt is invalid", "recording"
        ) from None


def recompute(record, adapter):
    """The caller selects installed code; an archive never chooses executable code."""
    verify_record(record)
    if record["status"] != "completed":
        raise ProtocolError(
            "AGGREGATE_UNAVAILABLE", "No completed aggregate can be recomputed", "unsupported"
        )
    result = adapter(deepcopy(record["payload"]))
    validate("aggregate_result", result)
    if identity(result) != record["result_digest"]:
        raise ProtocolError(
            "AGGREGATE_MISMATCH", "Native aggregate differs from its recorded result", "grading"
        )
    return result


def public_record(record):
    verify_record(record)
    result = record.get("result", {})
    return {
        "evaluation_id": record["evaluation_id"],
        "status": record["status"],
        "origin": record.get("origin"),
        "conditions_digest": record.get("conditions_digest"),
        "population_digest": record.get("population_digest"),
        "result_digest": record.get("result_digest"),
        "expected": result.get("expected", record.get("payload", {}).get("expected")),
        "observed": result.get("observed", len(record.get("members", []))),
        "complete": bool(result) and result["expected"] == result["observed"],
        "metrics": result.get("metrics", []),
        "reason": record.get("reason"),
    }
