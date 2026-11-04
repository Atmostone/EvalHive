import copy
import json
from pathlib import Path

import pytest
from evalhive_harness.protocol import (
    MAX_LINE_BYTES,
    ProtocolError,
    command,
    decode_line,
    encode_line,
    response,
    validate,
    validate_run,
)

FIXTURES = json.loads((Path(__file__).parents[2] / "fixtures/conformance.json").read_text())


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda c: c["name"])
def test_shared_contract(case):
    if case["error"]:
        with pytest.raises(ProtocolError) as exc:
            validate(case["kind"], case["value"])
        assert exc.value.code == case["error"]
    else:
        assert validate(case["kind"], case["value"]) == case["value"]
        if case["kind"] == "wire_message":
            assert decode_line(encode_line(case["value"])) == case["value"]


def test_configuration_is_connector_owned_and_checked_before_start():
    manifest, run = copy.deepcopy(FIXTURES["manifest"]), copy.deepcopy(FIXTURES["run_spec"])
    assert validate_run(manifest, run) == run
    run["config"]["unsupported_native_flag"] = True
    with pytest.raises(ProtocolError, match="Configuration") as exc:
        validate_run(manifest, run)
    assert exc.value.code == "INVALID_CONFIG"
    del run["config"]["unsupported_native_flag"]
    run["required_capabilities"] = ["approval"]
    with pytest.raises(ProtocolError) as exc:
        validate_run(manifest, run)
    assert exc.value.code == "UNSUPPORTED_CAPABILITY"


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://invalid.example.test/schema"},
        {"type": "invented"},
        {"$schema": "http://json-schema.org/draft-07/schema#"},
    ],
)
def test_invalid_or_remote_configuration_schema_is_not_loaded(schema):
    manifest = {**FIXTURES["manifest"], "config_schema": schema}
    with pytest.raises(ProtocolError) as exc:
        validate_run(manifest, FIXTURES["run_spec"])
    assert exc.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 10**1000, (1, 2), "\ud800"])
def test_non_portable_values_are_rejected(value):
    with pytest.raises(ProtocolError):
        response("request", value)


def test_cycles_and_depth_are_rejected():
    value = []
    value.append(value)
    with pytest.raises(ProtocolError):
        response("request", value)
    deep = []
    for _ in range(66):
        deep = [deep]
    with pytest.raises(ProtocolError):
        response("request", deep)


def test_framing_is_utf8_lf_with_size_bound():
    message = response("r", "Hello\u2028🌍\u2029\n")
    assert decode_line(encode_line(message)) == message
    assert decode_line(encode_line(message)[:-1] + b"\r\n") == message
    for value in [b"{}", b"{}\n{}\n", b"\xff\n", b'{"x":NaN}\n']:
        with pytest.raises(ProtocolError):
            decode_line(value)
    with pytest.raises(ProtocolError) as exc:
        encode_line(response("r", "x" * MAX_LINE_BYTES))
    assert exc.value.code == "FRAME_TOO_LARGE"
    assert decode_line(encode_line(command("r", "describe")))["id"] == "r"
