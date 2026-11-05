import copy
import json
from pathlib import Path

import pytest
from evalhive_benchmark import (
    ProtocolError,
    command,
    decode_line,
    encode_line,
    validate,
    validate_spec,
)
from evalhive_benchmark.protocol import MAX_LINE_BYTES, validate_config

ROOT = Path(__file__).parents[2]
FIXTURE = json.loads((ROOT / "fixtures/conformance.json").read_text())


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
def test_shared_contract(case):
    if case["error"]:
        with pytest.raises(ProtocolError) as caught:
            validate(case["kind"], case["value"])
        assert caught.value.code == case["error"]
    else:
        assert validate(case["kind"], case["value"]) == case["value"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 2**53, "\ud800", (1, 2)])
def test_nonportable_json(value):
    with pytest.raises(ProtocolError):
        encode_line(
            {"protocol_version": "1.0", "type": "response", "id": "x", "ok": True, "result": value}
        )


def test_framing_and_unicode():
    value = command("frame", "shutdown", {"reason": "Unicode\u2028🌍\u2029\n"})
    assert decode_line(encode_line(value)) == value
    assert decode_line(encode_line(value)[:-1] + b"\r\n") == value
    for frame in (b"{}", b"{}\n{}\n", b"\xff\n", b'{"value":NaN}\n'):
        with pytest.raises(ProtocolError):
            decode_line(frame)
    with pytest.raises(ProtocolError, match="size limit"):
        encode_line(
            {
                "protocol_version": "1.0",
                "type": "response",
                "id": "x",
                "ok": True,
                "result": "x" * MAX_LINE_BYTES,
            }
        )


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://example.invalid/schema"},
        {"$defs": {"unused": {"$ref": "https://example.invalid/schema"}}},
        {"type": "invented"},
        {"$schema": "http://json-schema.org/draft-07/schema#"},
    ],
)
def test_remote_or_invalid_config_schema(schema):
    with pytest.raises(ProtocolError) as caught:
        validate_config(schema, {})
    assert caught.value.code == "INVALID_CONFIG"


def test_configuration_and_credential_references_are_separate():
    spec = copy.deepcopy(FIXTURE["spec"])
    assert validate_spec(FIXTURE["manifest"], spec) == spec
    spec["config"]["api_key"] = "private-example-value"
    with pytest.raises(ProtocolError) as caught:
        validate_spec(FIXTURE["manifest"], spec)
    assert "private-example-value" not in str(caught.value)
    del spec["config"]["api_key"]
    spec["models"]["user_simulator"] = {
        "name": "example",
        "endpoint": "https://example.invalid/v1",
        "credential_ref": "private",
    }
    with pytest.raises(ProtocolError) as caught:
        validate_spec(FIXTURE["manifest"], spec)
    assert caught.value.code == "CREDENTIAL_REQUIRED"
    spec["credential_refs"]["private"] = "secret-version-1"
    validate_spec(FIXTURE["manifest"], spec)
    for endpoint in (
        "https://user:secret@example.invalid/v1",
        "https://example.invalid/v1?api_key=secret",
        "file:///tmp/model",
    ):
        spec["models"]["user_simulator"]["endpoint"] = endpoint
        with pytest.raises(ProtocolError) as caught:
            validate_spec(FIXTURE["manifest"], spec)
        assert caught.value.code == "INVALID_CONFIG"
        assert "api_key" not in str(caught.value)


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("duplicate", "DUPLICATE_RESOURCE"),
        ("missing", "MISSING_RESOURCE"),
        ("cycle", "RESOURCE_CYCLE"),
        ("output", "MISSING_RESOURCE"),
    ],
)
def test_environment_dependency_graph(mutation, code):
    plan = copy.deepcopy(FIXTURE["plan"])
    if mutation == "duplicate":
        plan["resources"].append(copy.deepcopy(plan["resources"][0]))
    elif mutation == "missing":
        plan["resources"][0]["depends_on"] = ["absent"]
    elif mutation == "cycle":
        plan["resources"][0]["depends_on"] = [plan["resources"][0]["id"]]
    else:
        plan["outputs"][0]["resource_id"] = "absent"
    with pytest.raises(ProtocolError) as caught:
        validate("environment_plan", plan)
    assert caught.value.code == code


def test_metric_identity_and_unknown_values():
    result = copy.deepcopy(FIXTURE["evaluation"])
    result["metrics"][0]["value"] = None
    validate("evaluation_result", result)
    result["metrics"].append(copy.deepcopy(result["metrics"][0]))
    with pytest.raises(ProtocolError) as caught:
        validate("evaluation_result", result)
    assert caught.value.code == "DUPLICATE_METRIC"


def test_cycles_and_depth_are_bounded():
    value = []
    value.append(value)
    with pytest.raises(ProtocolError):
        validate("response", value)
    deep = []
    for _ in range(66):
        deep = [deep]
    with pytest.raises(ProtocolError):
        validate("response", deep)
